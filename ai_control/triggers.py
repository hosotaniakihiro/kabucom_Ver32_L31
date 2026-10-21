# ============================================================
# File   : ai_control/triggers.py
# Purpose:
#   - ログ行から「Codex に解析させるべき異常」を検出する
#   - 同じ症状を何度も解析しないための signature / cooldown を持つ
# ------------------------------------------------------------
# 方針:
#   ログ 1 行ごとに AI を呼ぶことはしない。1 秒に数百行出るため、
#   料金・速度・誤判定のどれをとっても成立しない。
#   ここで機械的に絞り込み、Codex には「怪しい 1 件」だけを渡す。
# ============================================================

from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional


# 先頭のタイムスタンプ "2026-08-28 15:23:01,123 " を落とす
_TS_HEAD = re.compile(r"^\s*\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*")
# 可変部分の正規化
_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_NUMBER = re.compile(r"\d+")


def normalize_for_signature(line: str) -> str:
    s = _TS_HEAD.sub("", line)
    s = _HEX_ADDR.sub("0xA", s)
    s = _NUMBER.sub("N", s)
    return " ".join(s.split())[:300]


@dataclass
class TriggerHit:
    name: str
    severity: str
    prefix: str
    file_name: str
    line: str
    detected_at: float
    signature: str


@dataclass
class _BurstState:
    stamps: Deque[float] = field(default_factory=deque)


class TriggerEngine:
    """設定された正規表現でログ行を判定する。"""

    def __init__(self, trigger_defs: List[dict], exclude_patterns: Optional[List[str]] = None) -> None:
        # 既知の良性行。トリガ判定より先に落とす。
        self.excludes = [re.compile(p) for p in (exclude_patterns or [])]
        self.defs = []
        for d in trigger_defs:
            self.defs.append(
                {
                    "name": d["name"],
                    "severity": d.get("severity", "MEDIUM"),
                    "regex": re.compile(d["pattern"]),
                    "min_count": int(d.get("min_count", 1)),
                    "window_sec": float(d.get("window_sec", 60.0)),
                }
            )
        self._bursts: Dict[str, _BurstState] = {}

    def feed(self, prefix: str, file_name: str, line: str) -> Optional[TriggerHit]:
        now = time.time()
        for ex in self.excludes:
            if ex.search(line):
                return None
        for d in self.defs:
            if not d["regex"].search(line):
                continue

            if d["min_count"] <= 1:
                return self._make_hit(d, prefix, file_name, line, now)

            # バースト型: 窓内で min_count 回そろって初めてトリガ
            key = f"{d['name']}::{prefix}"
            st = self._bursts.setdefault(key, _BurstState())
            st.stamps.append(now)
            cutoff = now - d["window_sec"]
            while st.stamps and st.stamps[0] < cutoff:
                st.stamps.popleft()
            if len(st.stamps) >= d["min_count"]:
                st.stamps.clear()
                return self._make_hit(d, prefix, file_name, line, now)
            return None
        return None

    def _make_hit(self, d: dict, prefix: str, file_name: str, line: str, now: float) -> TriggerHit:
        sig_src = f"{d['name']}|{prefix}|{normalize_for_signature(line)}"
        signature = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()[:16]
        return TriggerHit(
            name=d["name"],
            severity=d["severity"],
            prefix=prefix,
            file_name=file_name,
            line=line,
            detected_at=now,
            signature=signature,
        )


class RateLimiter:
    """同じ症状の再解析と、1時間あたりの解析回数を抑える。"""

    def __init__(
        self,
        *,
        cooldown_sec_per_signature: float,
        max_analyses_per_hour: int,
        max_same_signature: int,
    ) -> None:
        self.cooldown = float(cooldown_sec_per_signature)
        self.max_per_hour = int(max_analyses_per_hour)
        self.max_same = int(max_same_signature)
        self.last_seen: Dict[str, float] = {}
        self.total_count: Dict[str, int] = {}
        self.recent: Deque[float] = deque()

    def load_state(self, state: dict) -> None:
        self.last_seen = {k: float(v) for k, v in (state.get("last_seen") or {}).items()}
        self.total_count = {k: int(v) for k, v in (state.get("total_count") or {}).items()}
        self.recent = deque(float(x) for x in (state.get("recent") or []))

    def dump_state(self) -> dict:
        return {
            "last_seen": self.last_seen,
            "total_count": self.total_count,
            "recent": list(self.recent),
        }

    def check(self, signature: str) -> tuple[bool, str]:
        """(通してよいか, 理由) を返す。"""
        now = time.time()

        cutoff = now - 3600.0
        while self.recent and self.recent[0] < cutoff:
            self.recent.popleft()
        if len(self.recent) >= self.max_per_hour:
            return False, f"rate limit: {self.max_per_hour} analyses/hour reached"

        last = self.last_seen.get(signature)
        if last is not None and now - last < self.cooldown:
            remain = int(self.cooldown - (now - last))
            return False, f"cooldown: same signature seen {remain}s ago"

        if self.total_count.get(signature, 0) >= self.max_same:
            return False, (
                f"SAFE_STOP: signature analyzed {self.max_same} times without resolution"
            )

        return True, "ok"

    def commit(self, signature: str) -> None:
        now = time.time()
        self.last_seen[signature] = now
        self.total_count[signature] = self.total_count.get(signature, 0) + 1
        self.recent.append(now)
