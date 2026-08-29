# ============================================================
# File   : ai_control/codex_client.py
# Purpose:
#   - codex exec を非対話で呼び、指示書 JSON を受け取る
# ------------------------------------------------------------
# 使うフラグ (codex-cli 0.150.1 で実挙動を確認済み):
#   -s read-only        診断役はファイルを書けない。指示文ではなく実行環境で縛る
#   -C <dir>            作業ルートをプロジェクトに固定
#   --output-schema     最終応答を task_schema.json の形に強制する
#   -o <file>           最終メッセージをファイルへ書き出す
#   --color never       ANSI エスケープを混ぜない
#   -                   プロンプトは stdin から渡す (長文・日本語のため)
#
# 既定の approval policy は never。対話で止まることはない。
# ============================================================

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


@dataclass
class CodexResult:
    ok: bool
    task: Optional[dict]
    raw: str
    error: str = ""
    returncode: int = 0


class CodexClient:
    def __init__(
        self,
        *,
        bin_path: str,
        project_dir: Path,
        schema_path: Path,
        sandbox: str = "read-only",
        timeout_sec: float = 420.0,
        model: str = "",
        out_dir: Optional[Path] = None,
    ) -> None:
        # Windows では codex は codex.CMD というシムなので、PATH 上の実体まで
        # 解決しておかないと subprocess が [WinError 2] で落ちる。
        resolved = shutil.which(bin_path) if not Path(bin_path).is_file() else bin_path
        self.bin_path = resolved or bin_path
        self.project_dir = Path(project_dir)
        self.schema_path = Path(schema_path)
        self.sandbox = sandbox
        self.timeout_sec = float(timeout_sec)
        self.model = model or ""
        # 出力用の一時ファイルは作業ルートとは別に置く。
        # project_dir は診断用 worktree を指すことがあり、そこに
        # ai_control/locks があるとは限らない（gitignore 済み）。
        # 既定はシステムの一時ディレクトリ（ローカル・高速）。
        self.out_dir = Path(out_dir) if out_dir else None

    def diagnose(self, prompt: str) -> CodexResult:
        out_dir = None
        if self.out_dir is not None:
            try:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                out_dir = str(self.out_dir)
            except OSError:
                out_dir = None
        out_fd = tempfile.NamedTemporaryFile(
            prefix="codex_out_", suffix=".json", delete=False, dir=out_dir
        )
        out_path = Path(out_fd.name)
        out_fd.close()

        cmd = [
            self.bin_path,
            "exec",
            "--skip-git-repo-check",
            "-C", str(self.project_dir),
            "-s", self.sandbox,
            "--color", "never",
            "--output-schema", str(self.schema_path),
            "-o", str(out_path),
        ]
        if self.model:
            cmd += ["-m", self.model]
        cmd.append("-")

        try:
            proc = subprocess.run(
                cmd,
                input=prompt.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired:
            out_path.unlink(missing_ok=True)
            return CodexResult(ok=False, task=None, raw="", error=f"codex timed out after {self.timeout_sec}s")
        except OSError as e:
            out_path.unlink(missing_ok=True)
            return CodexResult(ok=False, task=None, raw="", error=f"failed to launch codex: {e}")

        console = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""

        raw = ""
        try:
            raw = out_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        finally:
            out_path.unlink(missing_ok=True)

        if proc.returncode != 0 and not raw:
            return CodexResult(
                ok=False, task=None, raw=console, returncode=proc.returncode,
                error=f"codex exited {proc.returncode}",
            )

        task = self._parse(raw)
        if task is None:
            return CodexResult(
                ok=False, task=None, raw=raw or console, returncode=proc.returncode,
                error="codex output was not valid JSON",
            )
        return CodexResult(ok=True, task=task, raw=raw, returncode=proc.returncode)

    @staticmethod
    def _parse(raw: str) -> Optional[dict]:
        if not raw:
            return None
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            pass
        # スキーマ指定があれば素の JSON が返るはずだが、念のため
        m = _FENCE.search(raw)
        if m:
            try:
                v = json.loads(m.group(1))
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                v = json.loads(raw[start:end + 1])
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
        return None
