# ============================================================
# File   : ai_control/supervisor.py
# Purpose:
#   - 稼働中システムのログを監視し、異常を検出したら Codex に解析させ、
#     修正指示書 (inbox/task.json) を生成する
# Stage  : 1 / 5
# ------------------------------------------------------------
# この段階でやらないこと（意図的に）:
#   - Claude Code の起動          … 第2段階
#   - テスト実行                  … 第3段階
#   - main.py の停止・再起動      … 第4段階
#   - git rollback                … 第5段階
#
# つまり本体には一切触れません。読むだけです。
# ------------------------------------------------------------
# 使い方:
#   .venv\Scripts\python.exe ai_control\supervisor.py
#   .venv\Scripts\python.exe ai_control\supervisor.py --no-codex   # 配線確認
#   .venv\Scripts\python.exe ai_control\supervisor.py --once       # 1件出したら終了
#   .venv\Scripts\python.exe ai_control\supervisor.py --replay <logfile>
# ============================================================

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from codex_client import CodexClient           # noqa: E402
from logtail import LogTailer                  # noqa: E402
from policy import Policy                      # noqa: E402
from repair import Repairer                    # noqa: E402
from worktree import (                          # noqa: E402
    ReadOnlyWorktree, WorktreeError, uncommitted_diff, uncommitted_files,
)
from triggers import RateLimiter, TriggerEngine, TriggerHit  # noqa: E402


def _git_head(repo_dir: Path) -> tuple:
    """本番リポジトリの HEAD を返す。"""
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace").strip()


def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print("[" + ts + "] " + msg, flush=True)


@dataclass
class _Pending:
    """トリガ検出後、後続行がそろうまで待つ入れ物。"""
    hit: TriggerHit
    before: List[str]
    after: List[str] = field(default_factory=list)
    deadline: float = 0.0


class Supervisor:
    def __init__(self, cfg: dict, policy: Policy, args: argparse.Namespace) -> None:
        self.cfg = cfg
        self.policy = policy
        self.args = args

        paths = cfg["paths"]
        self.project_dir = Path(paths.get("project_dir") or HERE.parent).resolve()
        self.log_dir = Path(args.log_dir or paths["log_dir"])

        w = cfg["watch"]
        self.poll_interval = float(w["poll_interval_sec"])
        self.stall_seconds = float(w.get("stall_seconds") or 0)

        ev = cfg["evidence"]
        self.n_before = int(ev["context_lines_before"])
        self.n_after = int(ev["context_lines_after"])
        self.max_evidence_chars = int(ev["max_evidence_chars"])

        self.tailer = LogTailer(
            self.log_dir,
            w["prefixes"],
            suffix=w.get("suffix", ".log"),
            start_at=args.start_at or w.get("start_at", "end"),
            max_read_bytes=int(w["max_read_bytes_per_poll"]),
            rescan_interval_sec=float(w["rescan_interval_sec"]),
        )
        self.engine = TriggerEngine(cfg["triggers"], cfg.get("exclude_patterns"))

        lim = cfg["limits"]
        self.limiter = RateLimiter(
            cooldown_sec_per_signature=lim["cooldown_sec_per_signature"],
            max_analyses_per_hour=lim["max_analyses_per_hour"],
            max_same_signature=lim["max_same_signature"],
        )

        cx = cfg["codex"]

        # Codex にはローカルの checkout を見せる（NAS は読めない）
        self.diag_worktree = None
        diag_dir = (cx.get("worktree_dir") or "").strip()
        if diag_dir:
            try:
                self.diag_worktree = ReadOnlyWorktree(
                    self.project_dir,
                    Path(diag_dir),
                    float((cfg.get("repair") or {}).get("worktree_timeout_sec", 900)),
                )
            except WorktreeError as e:
                _log("!! 診断用 worktree を用意できません（本番ディレクトリを使います）: %s" % e)

        codex_root = self.diag_worktree.worktree_dir if self.diag_worktree else self.project_dir
        self.codex = CodexClient(
            bin_path=cx.get("bin", "codex"),
            project_dir=codex_root,
            schema_path=HERE / "task_schema.json",
            sandbox=cx.get("sandbox", "read-only"),
            timeout_sec=cx.get("timeout_sec", 420),
            model=cx.get("model", "") or "",
        )

        self.prompt_template = (HERE / "codex_prompt.md").read_text(encoding="utf-8")
        self.verify_template = (HERE / "verify_prompt.md").read_text(encoding="utf-8")

        vf = cx.get("verify") or {}
        self.verify_enabled = (not args.no_verify) and bool(vf.get("enabled", True))
        self.verify_timeout_sec = float(vf.get("timeout_sec", 420))
        self.verify_max_diff_chars = int(vf.get("max_diff_chars", 40000))

        buf_len = self.n_before + self.n_after + 200
        self.buffers: Dict[str, Deque[str]] = defaultdict(lambda: deque(maxlen=buf_len))
        self.pending: Dict[str, _Pending] = {}

        self.repairer = Repairer(
            here=HERE, project_dir=self.project_dir, cfg=cfg, policy=policy
        )
        self.repair_enabled = (not args.no_repair) and self.repairer.enabled

        self.state_path = HERE / "status" / "state.json"
        self.inbox_path = HERE / "inbox" / "task.json"
        self.history_dir = HERE / "history"
        self._load_state()

        self._last_stall_alert = 0.0
        self.dispatched = 0

    # -- state --------------------------------------------------

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log("state.json を読めませんでした（無視して続行）: " + str(e))
            return
        self.limiter.load_state(st.get("limiter") or {})
        _log("state.json を復元しました（既知シグネチャ %d 件）" % len(self.limiter.total_count))

    def _save_state(self) -> None:
        st = {
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "limiter": self.limiter.dump_state(),
            "watching": self.tailer.active_files(),
            "dropped_bytes": self.tailer.dropped_bytes(),
            "dispatched": self.dispatched,
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # -- メインループ --------------------------------------------

    def run(self) -> int:
        _log("project_dir = " + str(self.project_dir))
        _log("log_dir     = " + str(self.log_dir))
        if self.args.no_codex:
            _log("codex       = 無効 (--no-codex)")
        else:
            _log("codex       = " + self.codex.bin_path + " -s " + self.codex.sandbox)
            _log("codex root  = " + str(self.codex.project_dir))
            _log("自己検証    = " + ("有効（診断ごとに Codex を2回呼びます）" if self.verify_enabled else "無効"))
        if self.repair_enabled:
            _log("第2段階: 有効。GREEN/YELLOW は worktree 上で Claude Code が修正します。")
            _log("           本番への反映と main.py の再起動は行いません。")
        else:
            _log("第2段階: 無効。監視と診断のみ。")

        if not self.log_dir.exists():
            _log("!! ログディレクトリが見えません: " + str(self.log_dir))
            return 2

        try:
            while True:
                self._tick()
                if self.args.once and self.dispatched >= 1:
                    _log("--once: 1件処理したので終了します。")
                    return 0
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            _log("停止しました (Ctrl-C)")
            self._save_state()
            return 0

    def _tick(self) -> None:
        lines = self.tailer.poll()

        for ln in lines:
            self.buffers[ln.prefix].append(ln.text)

            # 保留中のトリガがあるなら、まず後続行として積む
            pend = self.pending.get(ln.prefix)
            if pend is not None:
                if len(pend.after) < self.n_after:
                    pend.after.append(ln.text)
                continue

            hit = self.engine.feed(ln.prefix, ln.file_name, ln.text)
            if hit is None:
                continue

            before = list(self.buffers[ln.prefix])[-(self.n_before + 1):]
            self.pending[ln.prefix] = _Pending(
                hit=hit,
                before=before,
                deadline=time.time() + max(15.0, self.poll_interval * 3),
            )
            _log("検出: %s [%s] in %s (sig=%s)" % (hit.name, hit.severity, ln.file_name, hit.signature))

        # 後続行がそろった／時間切れの保留を処理
        now = time.time()
        for prefix, pend in list(self.pending.items()):
            if len(pend.after) >= self.n_after or now >= pend.deadline:
                del self.pending[prefix]
                self._dispatch(pend)

        self._check_stall()
        self._save_state()

    def _check_stall(self) -> None:
        if self.stall_seconds <= 0:
            return
        idle = time.time() - self.tailer.last_growth_ts
        if idle < self.stall_seconds:
            return
        if time.time() - self._last_stall_alert < self.stall_seconds:
            return
        self._last_stall_alert = time.time()
        _log("!! 全ログが %d 秒伸びていません（停止の可能性）" % int(idle))

        synthetic = TriggerHit(
            name="stall",
            severity="HIGH",
            prefix="(all)",
            file_name=",".join(self.tailer.active_files().values()) or "(none)",
            line="no log growth for %d seconds" % int(idle),
            detected_at=time.time(),
            signature="stall-%d" % int(self.stall_seconds),
        )
        tail: List[str] = []
        for prefix, buf in self.buffers.items():
            tail.append("--- " + prefix + " の直近 ---")
            tail.extend(list(buf)[-40:])
        self._dispatch(_Pending(hit=synthetic, before=tail, after=[]))

    # -- 解析 ---------------------------------------------------

    def _sync_diag_worktree(self) -> str:
        """Codex に見せる checkout を本番 HEAD にそろえ、ズレの注意書きを返す。"""
        if self.diag_worktree is None:
            return (
                "> 注意: あなたの作業ディレクトリは本番そのものです。"
                "読み取りに失敗する場合は、その旨を root_cause に必ず書いてください。"
            )
        try:
            rc, head = _git_head(self.project_dir)
            if not head:
                return "> 注意: 本番 HEAD を取得できませんでした。"
            self.diag_worktree.sync(head)
        except WorktreeError as e:
            _log("!! 診断用 worktree の同期に失敗: %s" % e)
            return "> 注意: 診断用 checkout の同期に失敗しました。内容が古い可能性があります。"

        dirty = uncommitted_files(self.project_dir)
        note = [
            "> **あなたが読んでいるソースについて**",
            ">",
            "> 作業ディレクトリは本番そのものではなく、本番リポジトリの HEAD (`%s`) を" % head[:12],
            "> ローカルディスクへ checkout したものです。読み取りは自由に行えます。",
        ]
        if dirty:
            note += [
                ">",
                "> ただし、稼働中のプロセスが実行しているのは本番の作業ツリーであり、",
                "> 次のファイルには **HEAD に入っていない未コミットの変更があります**。",
                "> これらのファイルについては、あなたが読む内容と実際に動いている内容が",
                "> 食い違う可能性があります。断定する前にその点を考慮してください。",
                ">",
            ]
            for f in dirty[:30]:
                note.append("> - `%s`" % f)
            if len(dirty) > 30:
                note.append("> - ...ほか %d 件" % (len(dirty) - 30))
        return "\n".join(note)

    def _build_prompt(self, pend: _Pending) -> str:
        evidence = "\n".join(pend.before + ["", ">>> ここから後続 <<<", ""] + pend.after)
        if len(evidence) > self.max_evidence_chars:
            evidence = "...(前方を省略)...\n" + evidence[-self.max_evidence_chars:]
        h = pend.hit
        detected = dt.datetime.fromtimestamp(h.detected_at).isoformat(timespec="seconds")
        out = self.prompt_template
        out = out.replace("{{TRIGGER_NAME}}", h.name)
        out = out.replace("{{TRIGGER_SEVERITY}}", h.severity)
        out = out.replace("{{DETECTED_AT}}", detected)
        out = out.replace("{{LOG_FILE}}", h.file_name)
        out = out.replace("{{MATCHED_LINE}}", h.line)
        out = out.replace("{{EVIDENCE}}", evidence)
        out = out.replace("{{WORKTREE_NOTE}}", self._sync_diag_worktree())
        return out

    def _dispatch(self, pend: _Pending) -> None:
        h = pend.hit
        allowed, reason = self.limiter.check(h.signature)
        if not allowed:
            _log("見送り: %s sig=%s — %s" % (h.name, h.signature, reason))
            if reason.startswith("SAFE_STOP"):
                (HERE / "locks" / "SAFE_STOP").write_text(
                    dt.datetime.now().isoformat() + " " + h.signature + " " + reason + "\n",
                    encoding="utf-8",
                )
            return

        prompt = self._build_prompt(pend)

        if self.args.no_codex:
            _log("--no-codex: Codex を呼ばずにプロンプトだけ保存します (%d 文字)" % len(prompt))
            self._write_task(h, None, prompt, note="no-codex: 配線確認のみ")
            self.limiter.commit(h.signature)
            self.dispatched += 1
            return

        _log("Codex 解析開始: %s (prompt %d 文字)" % (h.name, len(prompt)))
        t0 = time.time()
        res = self.codex.diagnose(prompt)
        took = time.time() - t0

        if not res.ok:
            _log("!! Codex 失敗 (%.0fs): %s" % (took, res.error))
            self._write_task(h, None, prompt, note="codex failed: " + res.error, raw=res.raw)
            self.limiter.commit(h.signature)
            self.dispatched += 1
            return

        task = res.task or {}
        _log(
            "Codex 完了 (%.0fs): status=%s category=%s confidence=%s"
            % (took, task.get("status"), task.get("category"), task.get("confidence"))
        )

        verification = self._verify(task)

        doc = self._write_task(h, task, prompt, verification=verification)
        self.limiter.commit(h.signature)
        self.dispatched += 1

        if self.repair_enabled:
            self._run_repair(doc)

    # -- 第2段階 -------------------------------------------------

    def _run_repair(self, doc: dict) -> None:
        _log("修復判定を開始します。")
        try:
            out = self.repairer.repair(doc)
        except Exception as e:  # 修復側の事故で監視を止めない
            _log("!! 修復処理が例外で終了しました: %r" % (e,))
            return

        _log("修復結果: %s — %s" % (out.verdict, out.reason))
        if out.claude_invoked:
            _log("  Claude 起動: あり / 変更 %d ファイル" % len(out.changed_files))
            for f in out.changed_files[:10]:
                _log("    - " + f)
            if out.diff_stat:
                for line in out.diff_stat.strip().splitlines()[-1:]:
                    _log("  " + line.strip())
            if out.tests:
                _log("  テスト: %s" % ("合格" if out.tests.get("ok") else "不合格"))
            if out.branch:
                _log("  ブランチ: %s  commit=%s" % (out.branch, out.commit or "(なし)"))
        _log("  監査ログ: %s" % out.audit_path)

    # -- 診断の自己検証 --------------------------------------------

    def _uncommitted_section(self, named_files: List[str]) -> str:
        """稼働中の未コミット差分を、検証役へ渡す形にまとめる。

        診断が挙げたファイルの差分だけを渡すと、**関係するのに診断が
        挙げなかった**未コミットファイルが落ちる。実際 2026-08-29 の 401 では、
        原因に深く関わる kabu_api/token_bootstrap.py が診断の files に
        入っておらず、検証役に届かなかった。

        そこで「診断が挙げたもの」を先に、「それ以外の未コミット」を後ろに
        置き、上限まで詰める。上限に当たっても、重要な側は必ず残る。
        """
        dirty = uncommitted_files(self.project_dir)
        if not dirty:
            return (
                "本番の作業ツリーに未コミットの変更はありません。"
                "あなたが読む内容と稼働中の内容は一致します。"
            )

        named = [f for f in dirty if f in set(named_files)]
        others = [f for f in dirty if f not in set(named_files)]

        cap = self.verify_max_diff_chars
        parts = []
        used = 0
        if named:
            d = uncommitted_diff(self.project_dir, named, max_chars=cap)
            if d.strip():
                parts.append("# 診断が対象に挙げたファイルの未コミット差分\n" + d)
                used += len(d)
        if others and used < cap:
            d = uncommitted_diff(self.project_dir, others, max_chars=cap - used)
            if d.strip():
                parts.append(
                    "# 診断は挙げていないが、稼働中に変更されている他のファイル\n" + d
                )

        if not parts:
            return (
                "本番の作業ツリーには未コミットの変更がありますが、差分を取得できませんでした。\n"
                "変更されているファイル: " + ", ".join(dirty[:30])
            )

        return (
            "稼働中の本番作業ツリーには、あなたが読んでいる HEAD に入っていない\n"
            "変更があります。以下がその差分です。**この差分が診断を変えるか**を\n"
            "判断し、`uncommitted_impact` に書いてください。\n\n"
            "```diff\n" + "\n\n".join(parts) + "\n```"
        )

    def _verify(self, task: dict) -> Optional[dict]:
        """診断を Codex 自身に検証させ、確信度を付け直させる。

        1 回目の診断は、思い込みで CONFIRMED を書くことがある。実測では
        1 回の検証で主張 2 件が REFUTED になり、confidence 0.91 -> 0.72 に
        下がった（2026-08-29、401 の解析）。修正担当へ渡す前にここで落とす。

        検証するのは FIX_REQUIRED のときだけ。NO_ACTION / NEED_MORE_INFO は
        どのみち修正へ進まないので、Codex をもう 1 回呼ぶ意味がない。
        """
        if not self.verify_enabled:
            return None
        if (task or {}).get("status") != "FIX_REQUIRED":
            return None

        files = task.get("files") or []
        unc = self._uncommitted_section(files)

        def bullets(items):
            items = items or []
            return "\n".join("- " + str(x) for x in items) if items else "（なし）"

        vp = self.verify_template
        vp = vp.replace("{{SUMMARY}}", str(task.get("summary", "")))
        vp = vp.replace("{{CATEGORY}}", str(task.get("category", "")))
        vp = vp.replace("{{SEVERITY}}", str(task.get("severity", "")))
        vp = vp.replace("{{CONFIDENCE}}", str(task.get("confidence", "")))
        vp = vp.replace("{{ROOT_CAUSE}}", str(task.get("root_cause", "")))
        vp = vp.replace("{{FILES}}", bullets(files))
        vp = vp.replace("{{INSTRUCTIONS}}", bullets(task.get("instructions")))
        vp = vp.replace("{{EVIDENCE_LINES}}", "\n".join(task.get("evidence_lines") or []))
        vp = vp.replace("{{UNCOMMITTED_SECTION}}", unc)

        _log("診断の自己検証を開始します (prompt %d 文字)" % len(vp))
        t0 = time.time()
        res = self.codex.run(vp, HERE / "verify_schema.json", timeout_sec=self.verify_timeout_sec)
        took = time.time() - t0

        if not res.ok:
            _log("!! 検証に失敗 (%.0fs): %s — 診断は未検証のまま扱います" % (took, res.error))
            return {"verdict": "UNVERIFIED", "error": res.error}

        v = res.task or {}
        counts = {}
        for c in v.get("claims") or []:
            counts[c.get("status")] = counts.get(c.get("status"), 0) + 1
        _log(
            "検証完了 (%.0fs): %s  confidence %s -> %s  (%s)"
            % (
                took, v.get("verdict"), task.get("confidence"), v.get("revised_confidence"),
                " / ".join("%s %d" % (k, n) for k, n in sorted(counts.items())) or "主張なし",
            )
        )
        return v

    @staticmethod
    def _apply_verification(task: dict, v: Optional[dict]) -> dict:
        """検証結果を診断へ反映する。task は破壊的に更新する。"""
        if not v:
            # 検証を明示的に無効にしている場合。診断をそのまま使う。
            return task

        verdict = v.get("verdict")

        if verdict in (None, "UNVERIFIED"):
            # 検証しようとして失敗した。安全網が無い状態で自動修正へ進ませない。
            # 既定の閾値 (repair.min_confidence = 0.7) を下回る値に落として
            # 人間の判断へ回す。
            task["confidence_before_verification"] = task.get("confidence")
            try:
                task["confidence"] = min(float(task.get("confidence") or 0.0), 0.5)
            except (TypeError, ValueError):
                task["confidence"] = 0.5
            return task

        task["confidence_before_verification"] = task.get("confidence")

        if verdict == "DIAGNOSIS_WRONG":
            # 根幹が誤っていた。修正へ進ませない。
            task["status"] = "NEED_MORE_INFO"
            task["confidence"] = min(
                float(v.get("revised_confidence") or 0.0), 0.3
            )
        else:
            try:
                task["confidence"] = float(v.get("revised_confidence"))
            except (TypeError, ValueError):
                pass
        return task

    # -- 出力 ---------------------------------------------------

    def _write_task(
        self,
        hit: TriggerHit,
        task: Optional[dict],
        prompt: str,
        note: str = "",
        raw: str = "",
        verification: Optional[dict] = None,
    ) -> dict:
        # 検証結果は分類より先に反映する。confidence が下がると
        # 第2段階のゲートで落ちるため、順序を逆にしてはいけない。
        if task:
            task = self._apply_verification(task, verification)

        cls = None
        if task:
            cls = self.policy.classify(task.get("category", ""), task.get("files") or [])

        if task and cls and task.get("status") == "FIX_REQUIRED":
            next_action = cls["action"]
        elif task and task.get("status") == "NO_ACTION":
            next_action = "NONE"
        else:
            next_action = "HUMAN_REVIEW_REQUIRED"

        doc = {
            "schema_version": 1,
            "stage": 1,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "trigger": {
                "name": hit.name,
                "severity": hit.severity,
                "signature": hit.signature,
                "log_prefix": hit.prefix,
                "log_file": hit.file_name,
                "matched_line": hit.line,
                "detected_at": dt.datetime.fromtimestamp(hit.detected_at).isoformat(timespec="seconds"),
            },
            "codex": task,
            "verification": verification,
            "policy": cls,
            "note": note,
            "raw_output": raw[:4000] if raw else "",
            # 第2段階はここを見て Claude Code を呼ぶかどうかを決める
            "next_action": next_action,
        }

        payload = json.dumps(doc, ensure_ascii=False, indent=2)

        tmp = self.inbox_path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.inbox_path)

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = stamp + "_" + hit.name + "_" + hit.signature
        (self.history_dir / (base + ".json")).write_text(payload, encoding="utf-8")
        (self.history_dir / (base + ".prompt.md")).write_text(prompt, encoding="utf-8")

        level = cls["level"] if cls else "-"
        _log("書き出し: %s.json  分類=%s  next_action=%s" % (base, level, next_action))
        return doc

    # -- リプレイ（既存ログでの検証用） ---------------------------

    def replay(self, log_path: Path) -> int:
        _log("replay: " + str(log_path))
        if not log_path.exists():
            _log("!! ファイルがありません")
            return 2

        prefix = log_path.name.split("_")[0]
        buf: Deque[str] = deque(maxlen=self.n_before + self.n_after + 200)
        hits = 0

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()

        i = 0
        while i < len(lines):
            text = lines[i]
            buf.append(text)
            hit = self.engine.feed(prefix, log_path.name, text)
            if hit is not None:
                hits += 1
                _log("検出 #%d: %s [%s] line %d sig=%s" % (hits, hit.name, hit.severity, i + 1, hit.signature))
                if not self.args.list_only:
                    pend = _Pending(
                        hit=hit,
                        before=list(buf)[-(self.n_before + 1):],
                        after=lines[i + 1: i + 1 + self.n_after],
                    )
                    self._dispatch(pend)
                    if self.args.once and self.dispatched >= 1:
                        break
            i += 1

        _log("replay 完了: %d 件検出 / %d 件処理" % (hits, self.dispatched))
        self._save_state()
        return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="ログ監視 → Codex 診断 → task.json 生成（第1段階）")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--policy", default=str(HERE / "policy.yaml"))
    ap.add_argument("--log-dir", default="")
    ap.add_argument("--start-at", choices=["end", "beginning"], default="")
    ap.add_argument("--no-codex", action="store_true", help="Codex を呼ばずに配線だけ確認する")
    ap.add_argument("--once", action="store_true", help="1件処理したら終了する")
    ap.add_argument("--replay", default="", help="既存のログファイルで検証する")
    ap.add_argument("--list-only", action="store_true", help="replay で検出だけして解析しない")
    ap.add_argument("--no-verify", action="store_true", help="診断の自己検証を行わない")
    ap.add_argument("--no-repair", action="store_true", help="第2段階（Claude Code 修正）を行わない")
    ap.add_argument("--repair-from", default="", help="既存の task.json を読んで修復だけ実行する")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    pol = Policy(yaml.safe_load(Path(args.policy).read_text(encoding="utf-8")))

    sup = Supervisor(cfg, pol, args)

    if args.repair_from:
        doc = json.loads(Path(args.repair_from).read_text(encoding="utf-8"))
        _log("repair-from: " + args.repair_from)
        sup._run_repair(doc)
        return 0

    if args.replay:
        return sup.replay(Path(args.replay))
    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())
