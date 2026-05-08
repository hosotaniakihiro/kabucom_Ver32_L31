# ============================================================
# File   : trading/entry/summary_ai/entry_dedupe_guard.py
# Version: PRODUCTION-STABLE-REV1.1-FAILURE-RETRY-SAFE
# Purpose:
#   同一symbolの短時間二重ENTRYを防ぐ
# ------------------------------------------------------------
# Notes:
#   - runner 側は AI_OK 時点で mark_entry_attempt() を呼ぶ構造。
#   - その後 entry_controller / order_builder / API で失敗しても、
#     従来は300秒間 retry が止まり、エントリーが発火しないように見えた。
#   - そのため既定 cooldown を短くし、環境変数で調整可能にする。
#   - 実際の二重発注防止は entry_controller の symbol lock / open position /
#     pending identity / trade restriction 側でも守る。
# ============================================================

from __future__ import annotations

import datetime as dt
import math
import os
from threading import RLock


_lock = RLock()
_last_entry_ts: dict[str, dt.datetime] = {}


DEFAULT_COOLDOWN_SEC = 30


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        x = int(float(v))
        return x if math.isfinite(float(x)) and x >= 0 else int(default)
    except Exception:
        return int(default)


def _resolve_cooldown_sec(cooldown_sec: int | None) -> int:
    """
    優先順位:
      1. SUMMARY_AI_ENTRY_DEDUPE_COOLDOWN_SEC
      2. ENTRY_DEDUPE_COOLDOWN_SEC
      3. 呼び出し側引数
      4. DEFAULT_COOLDOWN_SEC

    runner.py は cooldown_sec=300 を渡してくるが、AI_OK時点でmarkされるため、
    発注失敗後も5分止まる副作用が大きい。環境変数未設定時は30秒に丸める。
    """
    if os.environ.get("SUMMARY_AI_ENTRY_DEDUPE_COOLDOWN_SEC") is not None:
        return _env_int("SUMMARY_AI_ENTRY_DEDUPE_COOLDOWN_SEC", DEFAULT_COOLDOWN_SEC)

    if os.environ.get("ENTRY_DEDUPE_COOLDOWN_SEC") is not None:
        return _env_int("ENTRY_DEDUPE_COOLDOWN_SEC", DEFAULT_COOLDOWN_SEC)

    try:
        requested = int(cooldown_sec) if cooldown_sec is not None else DEFAULT_COOLDOWN_SEC
    except Exception:
        requested = DEFAULT_COOLDOWN_SEC

    # AI_OK時点markの副作用対策。明示ENVが無い場合は最大30秒に抑える。
    return max(0, min(requested, DEFAULT_COOLDOWN_SEC))


def can_attempt_entry(
    symbol: str,
    *,
    cooldown_sec: int = 300,
    now: dt.datetime | None = None,
) -> tuple[bool, str]:
    if not symbol:
        return False, "empty symbol"

    now = now or dt.datetime.now()
    cooldown = _resolve_cooldown_sec(cooldown_sec)

    if cooldown <= 0:
        return True, "dedupe disabled"

    with _lock:
        prev = _last_entry_ts.get(symbol)

        if prev is not None:
            elapsed = (now - prev).total_seconds()
            if elapsed < cooldown:
                return False, f"cooldown active elapsed={elapsed:.1f}s < {cooldown}s"

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
