# -*- coding: utf-8 -*-
"""
Strict final recent-liquidity guard for entry_handler.

Purpose
-------
Even if RANKING / SUMMARY AI / TONOSAMA candidate filters are relaxed or a
rescue patch keeps a symbol alive, the actual order-dispatch layer must be
fail-closed for thin names.

V2:
- If summaryYYYYMMDD.db is stale but the order came from a fresh pending
  SUMMARY_AI / SUMMARY entry row, validate the row's own volume/turnover and
  allow only when it still satisfies the strict recent-liquidity thresholds.
- This fixes the case where PUSH/entry data is fresh enough for board recovery
  and ORDER_BUILD_OK, but final send is blocked by an old summary DB row.
- Missing/low liquidity still blocks. This is not a fail-open.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)
VERSION = "V2-FRESH-PENDING-FALLBACK-ON-STALE-SUMMARY-DB"
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


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            ss = s[:-2]
            if ss.isdigit():
                return ss
        return s
    except Exception:
        return ""


def _parse_dt(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    try:
        # pandas.Timestamp support without importing pandas.
        if hasattr(value, "to_pydatetime"):
            x = value.to_pydatetime()
            if isinstance(x, dt.datetime):
                return x.replace(tzinfo=None)
    except Exception:
        pass
    s = str(value).strip()
    if not s or s.lower() in {"nat", "none", "nan"}:
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


def _entry_timestamp(entry: dict[str, Any]) -> Optional[dt.datetime]:
    # created_at/updated_at represents when this candidate was built from the fresh
    # in-memory/PUSH path. Prefer it over the bar datetime if the summary DB is stale.
    for key in ("updated_at", "created_at", "entry_created_at", "received_at", "recv_time", "timestamp", "datetime"):
        x = _parse_dt(entry.get(key))
        if x is not None:
            return x
    return None


def _pending_entry_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    """Validate liquidity from fresh pending rows when summary DB is stale.

    This is still fail-closed: it only returns ok_read=True when the pending row
    has positive volume and turnover can be derived from row turnover or price*volume.
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return {"ok_read": False, "reason": "pending_symbol_missing", "source": "pending_entries"}
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", {}) or {}
        bucket = root.get(sym) or root.get(str(symbol)) or []
        if isinstance(bucket, dict):
            bucket = [bucket]
        entries = [e for e in list(bucket or []) if isinstance(e, dict)]
    except Exception as e:
        return {"ok_read": False, "reason": "pending_read_exception", "error": str(e), "source": "pending_entries"}
    if not entries:
        return {"ok_read": False, "reason": "pending_no_rows", "source": "pending_entries"}

    rows = []
    for e in entries[-max(1, int(bars)):]:
        volume = _f(e.get("volume") or e.get("latest_volume") or e.get("display_volume") or e.get("vol"), 0.0)
        price = _f(e.get("close_price") or e.get("price") or e.get("current_price") or e.get("close"), 0.0)
        turnover = _f(e.get("turnover") or e.get("trading_value") or e.get("display_turnover"), 0.0)
        if turnover <= 0 and price > 0 and volume > 0:
            turnover = price * volume
        t = _entry_timestamp(e)
        if volume > 0 and turnover > 0:
            rows.append({"volume": volume, "price": price, "turnover": turnover, "dt": t, "entry_type": e.get("entry_type"), "source": e.get("source")})
    if not rows:
        return {"ok_read": False, "reason": "pending_no_positive_liquidity", "source": "pending_entries", "pending_rows": len(entries)}
    latest = rows[-1]
    latest_dt = latest.get("dt")
    return {
        "ok_read": True,
        "rows": len(rows),
        "source": "pending_entries_fresh",
        "pending_rows": len(entries),
        "latest_dt": latest_dt.isoformat(sep=" ") if isinstance(latest_dt, dt.datetime) else None,
        "latest_close": latest.get("price"),
        "latest_volume": latest.get("volume"),
        "latest_turnover": latest.get("turnover"),
        "volume_sum": float(sum(r["volume"] for r in rows)),
        "turnover_sum": float(sum(r["turnover"] for r in rows)),
        "entry_type": latest.get("entry_type"),
        "entry_source": latest.get("source"),
    }


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

    def _detail_from(values: Any, *, fallback: bool = False, original_detail: dict[str, Any] | None = None) -> dict[str, Any]:
        rows = int(values.get("rows") or 0) if isinstance(values, dict) else 0
        volume_sum = _f(values.get("volume_sum") if isinstance(values, dict) else 0.0, 0.0)
        avg_volume = volume_sum / max(1, rows)
        latest_volume = _f(values.get("latest_volume") if isinstance(values, dict) else 0.0, 0.0)
        turnover_sum = _f(values.get("turnover_sum") if isinstance(values, dict) else 0.0, 0.0)
        latest_dt = _parse_dt(values.get("latest_dt") if isinstance(values, dict) else None)
        age_sec = (dt.datetime.now() - latest_dt).total_seconds() if latest_dt is not None else None
        out = {
            "symbol": symbol,
            "side": side,
            "bars": bars,
            "rows": rows,
            "latest_volume": latest_volume,
            "avg_volume": avg_volume,
            "volume_sum": volume_sum,
            "turnover_sum": turnover_sum,
            "latest_dt": values.get("latest_dt") if isinstance(values, dict) else None,
            "age_sec": age_sec,
            "min_latest_volume": min_latest_volume,
            "min_avg_volume": min_avg_volume,
            "min_turnover": min_turnover,
            "max_age_sec": max_age_sec,
            "fallback": fallback,
            **(values if isinstance(values, dict) else {}),
        }
        if original_detail is not None:
            out["original_detail"] = original_detail
        return out

    detail = _detail_from(v)

    if not isinstance(v, dict) or not bool(v.get("ok_read")):
        return (False, f"STRICT_FINAL_LIQ_READ_NG:{detail.get('reason')}", detail) if require_data else (True, "STRICT_FINAL_LIQ_READ_FAIL_OPEN", detail)
    if int(detail.get("rows") or 0) <= 0:
        return False, "STRICT_FINAL_LIQ_NO_RECENT_ROWS", detail
    if _parse_dt(v.get("latest_dt")) is None:
        return False, "STRICT_FINAL_LIQ_DATETIME_PARSE_NG", detail

    if detail.get("age_sec") is not None and float(detail["age_sec"]) > max_age_sec:
        pending_v = _pending_entry_liquidity_values(symbol, bars)
        pending_detail = _detail_from(pending_v, fallback=True, original_detail=detail)
        if isinstance(pending_v, dict) and bool(pending_v.get("ok_read")):
            pending_age = pending_detail.get("age_sec")
            pending_rows = int(pending_detail.get("rows") or 0)
            pending_latest_volume = _f(pending_detail.get("latest_volume"), 0.0)
            pending_avg_volume = _f(pending_detail.get("avg_volume"), 0.0)
            pending_turnover = _f(pending_detail.get("turnover_sum"), 0.0)
            if (
                pending_rows > 0
                and pending_age is not None
                and float(pending_age) <= max_age_sec
                and pending_latest_volume >= min_latest_volume
                and pending_avg_volume >= min_avg_volume
                and pending_turnover >= min_turnover
            ):
                logger.warning(
                    "[ENTRY FINAL LIQ GUARD] stale summary DB bypassed by fresh pending liquidity symbol=%s side=%s pending_age=%.1f summary_age=%.1f version=%s",
                    symbol,
                    side,
                    float(pending_age),
                    float(detail.get("age_sec") or -1),
                    VERSION,
                )
                return True, "STRICT_FINAL_LIQ_OK_PENDING_FRESH", pending_detail
        return False, f"STRICT_FINAL_LIQ_STALE:{float(detail.get('age_sec') or 0):.0f}>{max_age_sec:.0f}", detail

    if _f(detail.get("latest_volume"), 0.0) < min_latest_volume:
        return False, f"STRICT_FINAL_LIQ_LATEST_VOLUME_LOW:{_f(detail.get('latest_volume'), 0.0):.0f}<{min_latest_volume:.0f}", detail
    if _f(detail.get("avg_volume"), 0.0) < min_avg_volume:
        return False, f"STRICT_FINAL_LIQ_AVG_VOLUME_LOW:{_f(detail.get('avg_volume'), 0.0):.0f}<{min_avg_volume:.0f}", detail
    if _f(detail.get("turnover_sum"), 0.0) < min_turnover:
        return False, f"STRICT_FINAL_LIQ_TURNOVER_LOW:{_f(detail.get('turnover_sum'), 0.0):.0f}<{min_turnover:.0f}", detail

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
            "[ENTRY HANDLER STRICT LIQ] installed version=%s latest_vol>=%s avg_vol>=%s turnover>=%s max_age=%s pending_fallback=1",
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
