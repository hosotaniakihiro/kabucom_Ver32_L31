# ============================================================
# File   : trading/entry/summary_ai/entry_dedupe_guard.py
# Version: PRODUCTION-STABLE-REV1.0
# Purpose:
#   同一symbolの短時間二重ENTRYを防ぐ
# ============================================================

from __future__ import annotations

import datetime as dt
from threading import RLock


_lock = RLock()
_last_entry_ts: dict[str, dt.datetime] = {}


def can_attempt_entry(
    symbol: str,
    *,
    cooldown_sec: int = 300,
    now: dt.datetime | None = None,
) -> tuple[bool, str]:
    if not symbol:
        return False, "empty symbol"

    now = now or dt.datetime.now()

    with _lock:
        prev = _last_entry_ts.get(symbol)

        if prev is not None:
            elapsed = (now - prev).total_seconds()
            if elapsed < cooldown_sec:
                return False, f"cooldown active elapsed={elapsed:.1f}s < {cooldown_sec}s"

        return True, "ok"


def mark_entry_attempt(
    symbol: str,
    *,
    now: dt.datetime | None = None,
) -> None:
    if not symbol:
        return

    now = now or dt.datetime.now()

    with _lock:
        _last_entry_ts[symbol] = now


def clear_entry_guard() -> None:
    with _lock:
        _last_entry_ts.clear()