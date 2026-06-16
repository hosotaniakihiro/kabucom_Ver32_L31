# -*- coding: utf-8 -*-
"""
Strict final recent-liquidity guard for entry_handler.

Purpose
-------
Even if RANKING / SUMMARY AI / TONOSAMA candidate filters are relaxed or a
rescue patch keeps a symbol alive, the actual order-dispatch layer must be
fail-closed for thin names.

This patch wraps trading.handlers.entry_handler._final_recent_liquidity_ok and
adds non-negotiable checks immediately before order send:

- latest 1m volume >= 3,000 by default
- recent average 1m volume >= 3,000 by default
- recent N-bar turnover >= 1,000,000 yen by default
- latest summary row age <= 180 seconds by default
- unreadable/missing/stale data => blocked by default

Operator env overrides are still supported, but the defaults are intentionally
safer than the older entry_handler default where latest volume only needed to be
>= 1.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)
VERSION = "V1-STRICT-FINAL-RECENT-LIQUIDITY"
_INSTALLED = False
_ORIGINAL = None


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _parse_dt(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d%H%M%S",
    ):
        try:
            return dt.datetime.strptime(s, fmt)
        except Exception:
            pass
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _strict_final_recent_liquidity_ok(symbol: str, side: str):
    import trading.handlers.entry_handler as eh

    if not _env_bool("ENTRY_HANDLER_STRICT_RECENT_LIQ_GUARD_ENABLED", True):
        if callable(_ORIGINAL):
            return _ORIGINAL(symbol, side)
        return True, "STRICT_FINAL_LIQ_DISABLED", {}

    bars = max(1, _env_int("ENTRY_HANDLER_RECENT_LIQ_BARS", 5))
    min_latest_volume = _env_float(
        "ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME",
        _env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0),
    )
    min_avg_volume = _env_float(
        "ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME",
        _env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0),
    )
    min_turnover = _env_float(
        "ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN",
        _env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1000000.0),
    )
    max_age_sec = _env_float("ENTRY_HANDLER_STRICT_MAX_AGE_SEC", 180.0)
    require_data = _env_bool("ENTRY_HANDLER_RECENT_LIQ_REQUIRE_DATA", True)

    try:
        v = eh._recent_liquidity_values(symbol, bars)
    except Exception as e:
        detail = {"symbol": symbol, "side": side, "bars": bars, "reason": "exception", "error": str(e)}
        return (False, "STRICT_FINAL_LIQ_READ_EXCEPTION", detail) if require_data else (True, "STRICT_FINAL_LIQ_READ_EXCEPTION_FAIL_OPEN", detail)

    rows = int(v.get("rows") or 0) if isinstance(v, dict) else 0
    volume_sum = _f(v.get("volume_sum") if isinstance(v, dict) else 0.0, 0.0)
    avg_volume = volume_sum / max(1, rows)
    latest_volume = _f(v.get("latest_volume") if isinstance(v, dict) else 0.0, 0.0)
    turnover_sum = _f(v.get("turnover_sum") if isinstance(v, dict) else 0.0, 0.0)

    latest_dt = _parse_dt(v.get("latest_dt") if isinstance(v, dict) else None)
    age_sec = None
    if latest_dt is not None:
        age_sec = (dt.datetime.now() - latest_dt).total_seconds()

    detail = {
        "symbol": symbol,
        "side": side,
        "bars": bars,
        "rows": rows,
        "latest_volume": latest_volume,
        "avg_volume": avg_volume,
        "volume_sum": volume_sum,
        "turnover_sum": turnover_sum,
        "latest_dt": v.get("latest_dt") if isinstance(v, dict) else None,
        "age_sec": age_sec,
        "min_latest_volume": min_latest_volume,
        "min_avg_volume": min_avg_volume,
        "min_turnover": min_turnover,
        "max_age_sec": max_age_sec,
        **(v if isinstance(v, dict) else {}),
    }

    if not isinstance(v, dict) or not bool(v.get("ok_read")):
        return (False, f"STRICT_FINAL_LIQ_READ_NG:{detail.get('reason')}", detail) if require_data else (True, "STRICT_FINAL_LIQ_READ_FAIL_OPEN", detail)
    if rows <= 0:
        return False, "STRICT_FINAL_LIQ_NO_RECENT_ROWS", detail
    if latest_dt is None:
        return False, "STRICT_FINAL_LIQ_DATETIME_PARSE_NG", detail
    if age_sec is not None and age_sec > max_age_sec:
        return False, f"STRICT_FINAL_LIQ_STALE:{age_sec:.0f}>{max_age_sec:.0f}", detail
    if latest_volume < min_latest_volume:
        return False, f"STRICT_FINAL_LIQ_LATEST_VOLUME_LOW:{latest_volume:.0f}<{min_latest_volume:.0f}", detail
    if avg_volume < min_avg_volume:
        return False, f"STRICT_FINAL_LIQ_AVG_VOLUME_LOW:{avg_volume:.0f}<{min_avg_volume:.0f}", detail
    if turnover_sum < min_turnover:
        return False, f"STRICT_FINAL_LIQ_TURNOVER_LOW:{turnover_sum:.0f}<{min_turnover:.0f}", detail

    # Keep the original guard as an additional stricter layer. If the existing
    # handler requires 5-bar turnover >= 10,000,000, that still blocks here.
    if callable(_ORIGINAL):
        ok, reason, orig_detail = _ORIGINAL(symbol, side)
        if not ok:
            merged = dict(detail)
            merged["original_detail"] = orig_detail
            return False, reason, merged

    return True, "STRICT_FINAL_LIQ_OK", detail


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    if os.environ.get("DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PATCH", "").strip() == "1":
        logger.warning("[ENTRY HANDLER STRICT LIQ] disabled by env")
        return False
    try:
        # Fill safer defaults only when the operator has not explicitly set them.
        os.environ.setdefault("ENTRY_HANDLER_RECENT_LIQ_GUARD_ENABLED", "1")
        os.environ.setdefault("ENTRY_HANDLER_RECENT_LIQ_REQUIRE_DATA", "1")
        os.environ.setdefault("ENTRY_HANDLER_RECENT_LIQ_BARS", "5")
        os.environ.setdefault("ENTRY_HANDLER_RECENT_LIQ_MIN_LATEST_VOLUME", "3000")
        os.environ.setdefault("ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME", "3000")
        os.environ.setdefault("ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME", "3000")
        os.environ.setdefault("ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN", "1000000")
        os.environ.setdefault("ENTRY_HANDLER_STRICT_MAX_AGE_SEC", "180")

        import trading.handlers.entry_handler as eh

        current = getattr(eh, "_final_recent_liquidity_ok", None)
        if current is _strict_final_recent_liquidity_ok:
            _INSTALLED = True
            return True
        _ORIGINAL = current
        eh._final_recent_liquidity_ok = _strict_final_recent_liquidity_ok
        _INSTALLED = True
        logger.warning(
            "[ENTRY HANDLER STRICT LIQ] installed version=%s latest_vol>=%s avg_vol>=%s turnover>=%s max_age=%s",
            VERSION,
            os.environ.get("ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME"),
            os.environ.get("ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME"),
            os.environ.get("ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN"),
            os.environ.get("ENTRY_HANDLER_STRICT_MAX_AGE_SEC"),
        )
        return True
    except Exception:
        logger.exception("[ENTRY HANDLER STRICT LIQ] install failed")
        return False


__all__ = ["VERSION", "install"]
