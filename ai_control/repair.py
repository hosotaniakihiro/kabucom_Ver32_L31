# ============================================================
# File   : ai_control/repair.py
# Purpose:
#   - task.json を受け取り、条件を満たす場合だけ Claude Code に修正させる
# Stage  : 2 / 5
# ------------------------------------------------------------
# 流れ:
#   task.json
#     → ゲート判定 (status / confidence / policy / 回数制限 / lock)
#     → worktree を HEAD から作り直す
#     → Claude Code (Bash 不可・編集のみ) を非対話で起動
#     → 変更されたファイルを検査 ★ 禁止領域を触っていたら全部破棄
#     → compileall / import smoke
#     → 通れば auto-fix ブランチにコミット
#     → 監査ログ
#
# 本番の作業ツリーには一切書き込まない。main.py の停止・再起動もしない。
# 本番への反映は人が行う（第4段階で自動化する）。
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import List, Optional, Tuple

from audit import AuditLog
from claude_client import ClaudeClient, ClaudeResult
from policy import Policy, RED
from tests_runner import TestsRunner
from worktree import BRANCH_PREFIX, FixWorktree, WorktreeError


# 最終判定
V_NO_FIX = "NO_FIX"                       # そもそも修正対象でない
V_BLOCKED = "BLOCKED"                     # ゲートで止めた
V_HUMAN_REVIEW = "HUMAN_REVIEW_REQUIRED"  # RED
V_REFUSED = "CLAUDE_REFUSED"              # Claude が修正しなかった
V_VIOLATION = "POLICY_VIOLATION_REVERTED" # 禁止領域を触ったので破棄
V_TESTS_FAILED = "TESTS_FAILED"           # テスト不合格
V_READY = "READY_TO_APPLY"                # GREEN・テスト合格
V_DRY_RUN = "DRY_RUN_ONLY_HUMAN_APPLY"    # YELLOW・テスト合格だが人が反映
V_ERROR = "ERROR"


@dataclass
class RepairOutcome:
    verdict: str
    reason: str = ""
    claude_invoked: bool = False
    changed_files: List[str] = field(default_factory=list)
    diff_stat: str = ""
    tests: Optional[dict] = None
    branch: str = ""
    commit: str = ""
    audit_path: str = ""


class RepairLock:
    """二重起動と並列修正を防ぐ。"""

    def __init__(self, path: Path, timeout_sec: float) -> None:
        self.path = Path(path)
        self.timeout_sec = float(timeout_sec)
        self.acquired = False

    def acquire(self) -> Tuple[bool, str]:
        if self.path.exists():
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0.0
            if age < self.timeout_sec:
                try:
                    holder = self.path.read_text(encoding="utf-8").strip()
                except OSError:
                    holder = "(不明)"
                return False, "repair.lock が %.0f 秒前から存在する: %s" % (age, holder)
            # 古すぎる = 前回が異常終了した。奪う。
            try:
                self.path.unlink()
            except OSError:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "pid=%d started=%s" % (os.getpid(), dt.datetime.now().isoformat(timespec="seconds")),
            encoding="utf-8",
        )
        self.acquired = True
        return True, "ok"

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.acquired = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class Repairer:
    def __init__(
        self,
        *,
        here: Path,
        project_dir: Path,
        cfg: dict,
        policy: Policy,
    ) -> None:
        self.here = Path(here)
        self.project_dir = Path(project_dir)
        self.cfg = cfg
        self.policy = policy

        rp = cfg.get("repair") or {}
        self.enabled = bool(rp.get("enabled", True))
        self.min_confidence = float(rp.get("min_confidence", 0.7))
        self.allow_levels = set(rp.get("allow_levels") or ["GREEN", "YELLOW"])
        self.max_per_hour = int(rp.get("max_repairs_per_hour", 5))
        self.max_same_signature = int(rp.get("max_same_signature", 3))
        self.lock_timeout_sec = float(rp.get("lock_timeout_sec", 3600))
        self.apply_to_production = bool(rp.get("apply_to_production", False))
        self.worktree_timeout_sec = float(rp.get("worktree_timeout_sec", 900))

        wt_dir = rp.get("worktree_dir") or "../ai_fix_worktree"
        self.worktree_dir = (self.project_dir / wt_dir).resolve()

        cc = cfg.get("claude") or {}
        self.claude = ClaudeClient(
            bin_path=cc.get("bin", "claude"),
            timeout_sec=cc.get("timeout_sec", 900),
            max_turns=cc.get("max_turns", 30),
            permission_mode=cc.get("permission_mode", "acceptEdits"),
            model=cc.get("model", "") or "",
        )

        tc = cfg.get("tests") or {}
        python_exe = self.project_dir / (cfg["paths"].get("python") or ".venv/Scripts/python.exe")
        self.tests = TestsRunner(
            python_exe,
            compile_timeout_sec=tc.get("compile_timeout_sec", 300),
            import_timeout_sec=tc.get("import_timeout_sec", 90),
            import_smoke=bool(tc.get("import_smoke", True)),
            max_import_modules=int(tc.get("max_import_modules", 12)),
        )

        self.audit = AuditLog(self.here / "history")
        self.lock_path = self.here / "locks" / "repair.lock"
        self.prompt_template = (self.here / "claude_prompt.md").read_text(encoding="utf-8")

    # -- ゲート -------------------------------------------------

    def _gate(self, doc: dict) -> Tuple[bool, str, str]:
        """(通してよいか, verdict, 理由)"""
        if not self.enabled:
            return False, V_BLOCKED, "repair.enabled = false"

        task = doc.get("codex") or {}
        if not task:
            return False, V_NO_FIX, "Codex の診断がない"

        status = task.get("status")
        if status != "FIX_REQUIRED":
            return False, V_NO_FIX, "status=%s のため修正しない" % status

        cls = doc.get("policy") or {}
        level = cls.get("level")
        if level == RED:
            if cls.get("decided_by") == "paths":
                why = (cls.get("by_paths") or {}).get("reason", "")
            else:
                why = (cls.get("by_category") or {}).get("reason", "")
            return False, V_HUMAN_REVIEW, "policy=RED（%s）" % why
        if level not in self.allow_levels:
            return False, V_BLOCKED, "policy=%s は自動修正の対象外" % level

        try:
            conf = float(task.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < self.min_confidence:
            return False, V_BLOCKED, "confidence %.2f < %.2f のため Claude に渡さない" % (
                conf,
                self.min_confidence,
            )

        sig = (doc.get("trigger") or {}).get("signature") or ""
        if self.audit.count_recent(3600.0) >= self.max_per_hour:
            return False, V_BLOCKED, "1時間あたりの修正上限 %d 回に到達" % self.max_per_hour
        if self.audit.count_signature_total(sig) >= self.max_same_signature:
            return False, V_BLOCKED, "同一signature の修正が %d 回に到達（SAFE_STOP）" % (
                self.max_same_signature
            )

        return True, "", "ok"

    # -- プロンプト ----------------------------------------------

    def _build_prompt(self, doc: dict, base_commit: str) -> str:
        task = doc.get("codex") or {}
        trig = doc.get("trigger") or {}
        cls = doc.get("policy") or {}

        def bullets(items) -> str:
            items = items or []
            if not items:
                return "（なし）"
            return "\n".join("- " + str(x) for x in items)

        out = self.prompt_template
        out = out.replace("{{BASE_COMMIT}}", base_commit[:12])
        out = out.replace("{{POLICY_LEVEL}}", str(cls.get("level", "?")))
        out = out.replace("{{POLICY_ACTION}}", str(cls.get("action", "?")))
        out = out.replace("{{SUMMARY}}", str(task.get("summary", "")))
        out = out.replace("{{ROOT_CAUSE}}", str(task.get("root_cause", "")))
        out = out.replace("{{CATEGORY}}", str(task.get("category", "")))
        out = out.replace("{{SEVERITY}}", str(task.get("severity", "")))
        out = out.replace("{{CONFIDENCE}}", str(task.get("confidence", "")))
        out = out.replace("{{EVIDENCE_LINES}}", "\n".join(task.get("evidence_lines") or []))
        out = out.replace("{{FILES}}", bullets(task.get("files")))
        out = out.replace("{{INSTRUCTIONS}}", bullets(task.get("instructions")))
        out = out.replace("{{FORBIDDEN}}", bullets(task.get("forbidden")))
        out = out.replace("{{TRIGGER_NAME}}", str(trig.get("name", "")))
        out = out.replace("{{LOG_FILE}}", str(trig.get("log_file", "")))
        out = out.replace("{{MATCHED_LINE}}", str(trig.get("matched_line", "")))
        return out

    # -- 変更されたファイルの検査（事後チェック） -------------------

    def _violations(self, changed: List[str]) -> List[str]:
        """禁止領域のファイルが変更されていないか。Codex の申告ではなく実物を見る。"""
        bad = []
        for f in changed:
            cls = self.policy.classify("logging", [f])  # category は無害な値で固定し、パスだけで判定
            if cls["by_paths"]["level"] == RED:
                bad.append(f + " … " + cls["by_paths"]["reason"])
        return bad

    # -- 本体 ---------------------------------------------------

    def repair(self, doc: dict) -> RepairOutcome:
        sig = (doc.get("trigger") or {}).get("signature") or "nosig"
        record = {
            "stage": 2,
            "signature": sig,
            "trigger": doc.get("trigger"),
            "codex": doc.get("codex"),
            "confidence": (doc.get("codex") or {}).get("confidence"),
            "policy": doc.get("policy"),
            "claude_invoked": False,
        }

        ok, verdict, reason = self._gate(doc)
        if not ok:
            record["verdict"] = verdict
            record["reason"] = reason
            path = self.audit.write(record)
            return RepairOutcome(verdict=verdict, reason=reason, audit_path=str(path))

        with RepairLock(self.lock_path, self.lock_timeout_sec) as lock:
            got, lock_reason = lock.acquire()
            if not got:
                record["verdict"] = V_BLOCKED
                record["reason"] = lock_reason
                path = self.audit.write(record)
                return RepairOutcome(verdict=V_BLOCKED, reason=lock_reason, audit_path=str(path))
            return self._repair_locked(doc, record, sig)

    def _repair_locked(self, doc: dict, record: dict, sig: str) -> RepairOutcome:
        wt = FixWorktree(self.project_dir, self.worktree_dir, self.worktree_timeout_sec)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = BRANCH_PREFIX + stamp

        try:
            base = wt.head_commit()
            info = wt.prepare(branch, base)
        except (WorktreeError, OSError) as e:
            record["verdict"] = V_ERROR
            record["reason"] = "worktree の準備に失敗: %s" % e
            path = self.audit.write(record)
            return RepairOutcome(verdict=V_ERROR, reason=record["reason"], audit_path=str(path))

        record["base_commit"] = base
        record["branch"] = branch
        record["worktree"] = str(info.path)

        prompt = self._build_prompt(doc, base)
        record["claude_prompt_chars"] = len(prompt)
        record["claude_cmd"] = " ".join(self.claude.build_cmd()[:1] + ["-p", "..."])

        res: ClaudeResult = self.claude.fix(prompt, info.path)
        record["claude_invoked"] = True
        record["claude"] = {
            "ok": res.ok,
            "error": res.error,
            "duration_sec": round(res.duration_sec, 1),
            "num_turns": res.num_turns,
            "cost_usd": res.cost_usd,
            "text": res.text[:8000],
        }

        if not res.ok:
            record["verdict"] = V_ERROR
            record["reason"] = "Claude Code の実行に失敗: %s" % res.error
            path = self.audit.write(record)
            return RepairOutcome(
                verdict=V_ERROR, reason=record["reason"], claude_invoked=True, audit_path=str(path)
            )

        changed = wt.changed_files()
        record["changed_files"] = changed
        head = (res.text or "").strip()[:40].upper()

        if not changed:
            verdict = V_REFUSED
            reason = "Claude は変更を行わなかった（応答冒頭: %s）" % head
            record["verdict"] = verdict
            record["reason"] = reason
            path = self.audit.write(record)
            return RepairOutcome(
                verdict=verdict, reason=reason, claude_invoked=True, branch=branch,
                audit_path=str(path)
            )

        # ★ 事後チェック: 禁止領域を触っていたら全部破棄する
        violations = self._violations(changed)
        if violations:
            diff_text = wt.diff()
            record["violations"] = violations
            record["verdict"] = V_VIOLATION
            record["reason"] = "禁止領域のファイルが変更されたため破棄: " + "; ".join(violations)
            path = self.audit.write(record, diff_text)
            try:
                wt.prepare(branch, base)  # reset --hard で捨てる
            except WorktreeError:
                pass
            return RepairOutcome(
                verdict=V_VIOLATION, reason=record["reason"], claude_invoked=True,
                changed_files=changed, branch=branch, audit_path=str(path)
            )

        diff_text = wt.diff()
        diff_stat = wt.diff_stat()
        record["diff_stat"] = diff_stat

        report = self.tests.run(info.path, changed)
        record["tests"] = report.to_dict()

        if not report.ok:
            record["verdict"] = V_TESTS_FAILED
            record["reason"] = "テスト不合格（%s）" % report.summary()
            path = self.audit.write(record, diff_text)
            return RepairOutcome(
                verdict=V_TESTS_FAILED, reason=record["reason"], claude_invoked=True,
                changed_files=changed, diff_stat=diff_stat, tests=record["tests"],
                branch=branch, audit_path=str(path)
            )

        commit = wt.commit(
            "auto-fix(%s): %s" % (sig, (doc.get("codex") or {}).get("summary", "")[:60])
        )
        record["commit"] = commit

        level = (doc.get("policy") or {}).get("level")
        verdict = V_READY if level == "GREEN" else V_DRY_RUN
        record["verdict"] = verdict
        record["reason"] = "テスト合格（%s）。本番への反映は行っていない。" % report.summary()
        record["applied_to_production"] = False
        path = self.audit.write(record, diff_text)

        return RepairOutcome(
            verdict=verdict, reason=record["reason"], claude_invoked=True,
            changed_files=changed, diff_stat=diff_stat, tests=record["tests"],
            branch=branch, commit=commit or "", audit_path=str(path)
        )
