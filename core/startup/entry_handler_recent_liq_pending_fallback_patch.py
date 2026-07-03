# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-ENTRY-FINAL-LIQ-PENDING-ROW-FALLBACK"
_INSTALLED = False
_ORIGINAL = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        if v is None or str(v).strip() == "":
            return None
        s = str(v).strip()
        # pandas Timestamp string / ISO-like string
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return dt.datetime.strptime(s, fmt)
            except Exception:
                continue
    except Exception:
        return None
    return None


def _iter_pending_for_symbol(symbol: str):
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", {}) or {}
        sym = _norm_symbol(symbol)
        bucket = root.get(sym) or root.get(str(symbol)) or []
        if isinstance(bucket, dict):
            bucket = [bucket]
        if isinstance(bucket, list):
            for e in reversed(bucket):
                if isinstance(e, dict):
                    yield e
    except Exception:
        return


def _pending_liq_ok(symbol: str, side: str) -> tuple[bool, str, dict[str, Any]]:
    min_latest_volume = _env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_LATEST_VOLUME", 1.0)
    min_volume = _env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_VOLUME", _env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_TURNOVER_YEN", _env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 1000000.0))
    max_age = _env_float("ENTRY_HANDLER_RECENT_LIQ_MAX_AGE_SEC", _env_float("ENTRY_HANDLER_STRICT_LIQ_MAX_AGE_SEC", 180.0))
    now = dt.datetime.now()
    sym = _norm_symbol(symbol)

    best_detail: dict[str, Any] = {"symbol": sym, "side": side, "source": "pending_entry_row", "min_latest_volume": min_latest_volume, "min_volume": min_volume, "min_turnover": min_turnover, "max_age_sec": max_age}
    for row in _iter_pending_for_symbol(sym):
        row_sym = _norm_symbol(row.get("symbol") or sym)
        if row_sym != sym:
            continue
        row_side = str(row.get("side") or row.get("entry_decision") or row.get("ai_side") or "").upper()
        if row_side in {"BUY", "SELL"} and str(side or "").upper() in {"BUY", "SELL"} and row_side != str(side).upper():
            continue
        ts = None
        for k in ("datetime", "dt", "time", "created_at", "updated_at"):
            ts = _parse_dt(row.get(k))
            if ts is not None:
                break
        age = None if ts is None else max(0.0, (now - ts).total_seconds())
        price = _f(row.get("close_price") or row.get("price") or row.get("current_price") or row.get("close"), 0.0)
        vol = _f(row.get("volume") or row.get("vol") or row.get("latest_volume") or row.get("display_volume"), 0.0)
        turnover = _f(row.get("turnover") or row.get("trading_value") or row.get("turnover_yen") or row.get("display_turnover"), 0.0)
        if turnover <= 0 and price > 0 and vol > 0:
            turnover = price * vol
        detail = dict(best_detail)
        detail.update({"row_datetime": str(ts) if ts else None, "age_sec": age, "price": price, "latest_volume": vol, "volume_sum": vol, "turnover_sum": turnover, "entry_type": row.get("entry_type"), "row_source": row.get("source")})
        if age is None:
            best_detail = detail | {"ng": "pending_row_no_time"}
            continue
        if age > max_age:
            best_detail = detail | {"ng": f"pending_row_stale:{age:.1f}>{max_age:.1f}"}
            continue
        if vol < min_latest_volume:
            best_detail = detail | {"ng": f"pending_latest_volume_low:{vol:.0f}<{min_latest_volume:.0f}"}
            continue
        if vol < min_volume:
            best_detail = detail | {"ng": f"pending_volume_low:{vol:.0f}<{min_volume:.0f}"}
            continue
        if turnover < min_turnover:
            best_detail = detail | {"ng": f"pending_turnover_low:{turnover:.0f}<{min_turnover:.0f}"}
            continue
        logger.warning("[ENTRY FINAL LIQ PENDING FALLBACK] OK symbol=%s side=%s detail=%s version=%s", sym, side, detail, VERSION)
        return True, "FINAL_LIQ_PENDING_ROW_OK", detail
    return False, "FINAL_LIQ_PENDING_ROW_NG", best_detail


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_handler as eh
        cur = getattr(eh, "_final_recent_liquidity_ok", None)
        if not callable(cur):
            logger.warning("[ENTRY FINAL LIQ PENDING FALLBACK] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_entry_final_liq_pending_fallback_v1", False):
            _INSTALLED = True
            return True
        _ORIGINAL = getattr(cur, "_original", cur)

        @wraps(_ORIGINAL)
        def wrapped(symbol: str, side: str):
            ok, reason, detail = _ORIGINAL(symbol, side)
            if ok:
                return ok, reason, detail
            reason_s = str(reason or "")
            # Only rescue stale/missing DB reads with fresh pending row. Do not rescue real low-volume pending rows.
            rescue_candidates = (
                "STALE" in reason_s
                or "READ_NG" in reason_s
                or "NO_RECENT_ROWS" in reason_s
                or "LATEST_VOLUME_LOW" in reason_s
                or "VOLUME_LOW" in reason_s
                or "TURNOVER_LOW" in reason_s
            )
            p_ok, p_reason, p_detail = _pending_liq_ok(symbol, side)
            merged = dict(detail or {})
            merged["pending_row_check"] = p_detail
            if rescue_candidates and p_ok:
                logger.warning("[ENTRY FINAL LIQ PENDING FALLBACK] rescue symbol=%s side=%s original=%s pending=%s version=%s", symbol, side, reason, p_detail, VERSION)
                return True, p_reason, merged
            return ok, reason, merged

        wrapped._entry_final_liq_pending_fallback_v1 = True  # type: ignore[attr-defined]
        wrapped._original = _ORIGINAL  # type: ignore[attr-defined]
        eh._final_recent_liquidity_ok = wrapped
        _INSTALLED = True
        logger.warning("[ENTRY FINAL LIQ PENDING FALLBACK] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[ENTRY FINAL LIQ PENDING FALLBACK] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY FINAL LIQ PENDING FALLBACK] auto install failed")


__all__ = ["install", "VERSION"]
