# ============================================================
# File   : utils/market_session.py
# Version: Ver1.0-PRODUCTION-MARKET-SESSION-COMPAT-FINAL
# ------------------------------------------------------------
# ✔ is_market_open 提供
# ✔ orchestrator import 対応
# ✔ 日本市場時間対応
# ✔ 土日判定
# ✔ 昼休み判定
# ✔ timezone安全
# ✔ 祝日ライブラリ未導入でも動作
# ✔ 既存機能破壊ゼロ
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

logger = logging.getLogger(__name__)

JST = dt.timezone(dt.timedelta(hours=9))


def _to_jst(now: Optional[dt.datetime] = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(JST)

    if not isinstance(now, dt.datetime):
        return dt.datetime.now(JST)

    if now.tzinfo is None:
        return now.replace(tzinfo=JST)

    return now.astimezone(JST)


def _is_weekend(now: dt.datetime) -> bool:
    return now.weekday() >= 5


def _is_jpx_holiday(now: dt.datetime) -> bool:
    """
    祝日判定は optional。
    jpholiday が入っていれば使う。
    無ければ False 扱いにして安全側で継続。
    """
    try:
        import jpholiday  # type: ignore
        return bool(jpholiday.is_holiday(now.date()))
    except Exception:
        return False


def is_morning_session(now: Optional[dt.datetime] = None) -> bool:
    x = _to_jst(now)
    t = x.time()
    return dt.time(9, 0) <= t < dt.time(11, 30)


def is_afternoon_session(now: Optional[dt.datetime] = None) -> bool:
    x = _to_jst(now)
    t = x.time()
    return dt.time(12, 30) <= t < dt.time(15, 30)


def is_market_open(now: Optional[dt.datetime] = None) -> bool:
    """
    東証の通常立会を簡易判定。
    前場 09:00-11:30
    後場 12:30-15:30
    """
    x = _to_jst(now)

    if _is_weekend(x):
        return False

    if _is_jpx_holiday(x):
        return False

    if is_morning_session(x):
        return True

    if is_afternoon_session(x):
        return True

    return False


def is_lunch_break(now: Optional[dt.datetime] = None) -> bool:
    x = _to_jst(now)
    t = x.time()
    return dt.time(11, 30) <= t < dt.time(12, 30)


def is_before_open(now: Optional[dt.datetime] = None) -> bool:
    x = _to_jst(now)
    return x.time() < dt.time(9, 0)


def is_after_close(now: Optional[dt.datetime] = None) -> bool:
    x = _to_jst(now)
    return x.time() >= dt.time(15, 30)


def get_market_session(now: Optional[dt.datetime] = None) -> str:
    x = _to_jst(now)

    if _is_weekend(x):
        return "closed_weekend"

    if _is_jpx_holiday(x):
        return "closed_holiday"

    if is_morning_session(x):
        return "morning"

    if is_lunch_break(x):
        return "lunch_break"

    if is_afternoon_session(x):
        return "afternoon"

    if is_before_open(x):
        return "before_open"

    if is_after_close(x):
        return "after_close"

    return "closed"


def get_market_session_info(now: Optional[dt.datetime] = None) -> dict:
    x = _to_jst(now)
    return {
        "now_jst": x,
        "session": get_market_session(x),
        "is_market_open": is_market_open(x),
        "is_morning_session": is_morning_session(x),
        "is_afternoon_session": is_afternoon_session(x),
        "is_lunch_break": is_lunch_break(x),
        "is_before_open": is_before_open(x),
        "is_after_close": is_after_close(x),
        "is_weekend": _is_weekend(x),
        "is_holiday": _is_jpx_holiday(x),
    }


__all__ = [
    "is_market_open",
    "is_morning_session",
    "is_afternoon_session",
    "is_lunch_break",
    "is_before_open",
    "is_after_close",
    "get_market_session",
    "get_market_session_info",
]