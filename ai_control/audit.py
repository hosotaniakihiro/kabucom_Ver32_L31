# ============================================================
# File   : ai_control/audit.py
# Purpose:
#   - 1 件の修復サイクルを丸ごと監査ログへ残す
# ------------------------------------------------------------
# 記録先:
#   history/audit.jsonl              … 1 行 1 サイクル（一覧・集計用）
#   history/<stamp>_<sig>.audit.json … 同じ内容を整形して個別保存
#   history/<stamp>_<sig>.diff       … Claude が作った差分の原文
#
# AI に本番コードを触らせる以上、「何を根拠に、誰が、何を変えたか」が
# 後から追えないと運用できない。ここは削らない。
# ============================================================

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional


class AuditLog:
    def __init__(self, history_dir: Path) -> None:
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.history_dir / "audit.jsonl"

    def write(self, record: dict, diff_text: str = "") -> Path:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        sig = str(record.get("signature") or "nosig")
        base = "%s_%s" % (stamp, sig)

        record = dict(record)
        record.setdefault("timestamp", dt.datetime.now().isoformat(timespec="seconds"))
        record["audit_id"] = base

        if diff_text:
            diff_path = self.history_dir / (base + ".diff")
            diff_path.write_text(diff_text, encoding="utf-8")
            record["diff_file"] = diff_path.name

        detail_path = self.history_dir / (base + ".audit.json")
        detail_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return detail_path

    def count_recent(self, within_sec: float, only_signature: Optional[str] = None) -> int:
        """直近 within_sec 秒に実際に Claude を起動した回数を数える。"""
        if not self.jsonl_path.exists():
            return 0
        now = dt.datetime.now()
        n = 0
        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not rec.get("claude_invoked"):
                        continue
                    if only_signature and rec.get("signature") != only_signature:
                        continue
                    ts = rec.get("timestamp")
                    if not ts:
                        continue
                    try:
                        when = dt.datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                    if (now - when).total_seconds() <= within_sec:
                        n += 1
        except OSError:
            return 0
        return n

    def count_signature_total(self, signature: str) -> int:
        """そのシグネチャで通算何回 Claude を起動したか。"""
        if not self.jsonl_path.exists():
            return 0
        n = 0
        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("signature") == signature and rec.get("claude_invoked"):
                        n += 1
        except OSError:
            return 0
        return n
