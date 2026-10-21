# ============================================================
# File   : ai_control/claude_client.py
# Purpose:
#   - claude -p (非対話モード) を呼び、修正を実行させる
# ------------------------------------------------------------
# 使うフラグ (実挙動を確認済み):
#   -p                        非対話。応答を出して終了する
#   --output-format json      結果・コスト・ターン数を JSON で受け取る
#   --permission-mode acceptEdits   編集の確認プロンプトを出さない
#   --allowedTools            Read/Edit/Write/Grep/Glob だけ許可
#   --disallowedTools Bash    ★ コマンド実行を禁止する
#   --max-turns               暴走時の上限
#   --append-system-prompt    守らせる制約を system 側にも入れる
#
# Bash を禁止しているのが要点:
#   git 操作・テスト・再起動はすべて supervisor 側が握る。
#   Claude Code にできるのは「ファイルを読んで直す」ことだけになる。
# ============================================================

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# どんな task であっても Claude に必ず渡す禁止事項
HARD_CONSTRAINTS = """\
あなたは稼働中の株式自動売買システムの修正担当です。実弾で発注するシステムです。

絶対に守ること:
- Codex の診断は参考情報にすぎない。盲目的に従わないこと。
- まず対象コードを自分で読んで確認すること。
- 原因と修正案が一致していることを、自分で確かめること。
- 一致していない、または原因が不明確なら、修正せずに NEED_MORE_INFO と述べて終了すること。
- 必要最小限の変更だけを行うこと。
- 指示されていないリファクタリング・整形・改名を一切行わないこと。
- 既存の仕様を変更しないこと。
- 注文・売買・BUY/SELL・注文数量・注文価格・SOR・entry・exit・position・
  資金管理・利確・損切り・認証・APIキー・settings.ini・DB schema・migration には
  一切触れないこと。これらに触れる必要があるなら、修正せずにその旨を述べて終了すること。
- テストを通すためにテスト側を書き換えないこと。
- ファイルの削除、大規模な書き換えを行わないこと。
"""


@dataclass
class ClaudeResult:
    ok: bool
    text: str = ""
    raw: dict = field(default_factory=dict)
    error: str = ""
    returncode: int = 0
    duration_sec: float = 0.0
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None


class ClaudeClient:
    def __init__(
        self,
        *,
        bin_path: str = "claude",
        timeout_sec: float = 900.0,
        max_turns: int = 30,
        permission_mode: str = "acceptEdits",
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        model: str = "",
    ) -> None:
        resolved = shutil.which(bin_path) if not Path(bin_path).is_file() else bin_path
        self.bin_path = resolved or bin_path
        self.timeout_sec = float(timeout_sec)
        self.max_turns = int(max_turns)
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools or ["Read", "Edit", "Write", "Grep", "Glob"]
        self.disallowed_tools = disallowed_tools or ["Bash", "WebFetch", "WebSearch", "Task"]
        self.model = model or ""

    def build_cmd(self) -> List[str]:
        cmd = [
            self.bin_path,
            "-p",
            "--output-format", "json",
            "--permission-mode", self.permission_mode,
            "--max-turns", str(self.max_turns),
            "--allowedTools", " ".join(self.allowed_tools),
            "--disallowedTools", " ".join(self.disallowed_tools),
            "--append-system-prompt", HARD_CONSTRAINTS,
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def fix(self, prompt: str, cwd: Path) -> ClaudeResult:
        import time

        cmd = self.build_cmd()
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                input=prompt.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return ClaudeResult(
                ok=False,
                error="claude timed out after %.0fs" % self.timeout_sec,
                duration_sec=time.time() - t0,
            )
        except OSError as e:
            return ClaudeResult(ok=False, error="failed to launch claude: %s" % e)

        took = time.time() - t0
        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""

        data: Dict = {}
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return ClaudeResult(
                ok=False,
                text=out[:4000],
                error="claude output was not JSON (rc=%d) %s" % (proc.returncode, err[:300]),
                returncode=proc.returncode,
                duration_sec=took,
            )

        is_error = bool(data.get("is_error"))
        text = data.get("result") or data.get("text") or ""
        return ClaudeResult(
            ok=(proc.returncode == 0 and not is_error),
            text=text if isinstance(text, str) else json.dumps(text, ensure_ascii=False),
            raw={k: v for k, v in data.items() if k != "result"},
            error="" if not is_error else str(data.get("error") or "is_error=true"),
            returncode=proc.returncode,
            duration_sec=took,
            cost_usd=data.get("total_cost_usd"),
            num_turns=data.get("num_turns"),
        )
