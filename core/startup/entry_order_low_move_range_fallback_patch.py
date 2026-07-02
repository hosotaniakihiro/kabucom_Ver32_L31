from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1"
_INSTALLED = False


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return d
        return float(str(v).replace(",", ""))
    except Exception:
        return d


def _d(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return dict(v)
    try:
        if hasattr(v, "to_dict"):
            x = v.to_dict()
            return dict(x) if isinstance(x, dict) else {}
    except Exception:
        pass
    return {}


def _pick(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for n in names:
        try:
            v = row.get(n)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return None


def _allow(row: dict[str, Any], detail: dict[str, Any], symbol: str, source: str) -> bool:
    close = _f(_pick(row, ("close_price", "close", "price", "current_price")) or detail.get("close"), 0.0)
    if close <= 0:
        return False
    need = _f(os.getenv("ENTRY_ORDER_DAY_RANGE_FALLBACK_MIN_PCT"), _f(detail.get("min_range_pct"), _f(os.getenv("ENTRY_ORDER_MIN_RANGE_PCT"), 0.005)))
    rp = _f(_pick(row, ("range_pct", "day_range_pct", "intraday_range_pct")), 0.0)
    if rp > 1.0:
        rp = rp / 100.0
    if rp >= need:
        logger.warning("[ENTRY ORDER LOW MOVE RANGE FALLBACK] allow range_pct symbol=%s source=%s range=%.6f need=%.6f", symbol, source, rp, need)
        return True
    hi = _f(_pick(row, ("day_high", "DayHigh", "session_high", "high_day")), 0.0)
    lo = _f(_pick(row, ("day_low", "DayLow", "session_low", "low_day")), 0.0)
    if hi > 0 and lo > 0 and hi >= lo:
        dr = (hi - lo) / close
        if dr >= need:
            logger.warning("[ENTRY ORDER LOW MOVE RANGE FALLBACK] allow day_range symbol=%s source=%s close=%.4f high=%.4f low=%.4f range=%.6f need=%.6f", symbol, source, close, hi, lo, dr, need)
            return True
    return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_order_builder as eob
        cur = getattr(eob, "_low_move_hard_block", None)
        if not callable(cur):
            return False
        if getattr(cur, "_entry_order_low_move_range_fallback_v1", False):
            _INSTALLED = True
            return True
        orig = getattr(cur, "_original", cur)
        def patched(entry_row, *, symbol: str, source: str):
            ret = orig(entry_row, symbol=symbol, source=source)
            try:
                if isinstance(ret, dict) and ret.get("reason") == "LOW_MOVE_RANGE_TOO_SMALL":
                    row = _d(entry_row)
                    src = str(source or row.get("source") or "").upper()
                    et = str(row.get("entry_type") or "").upper()
                    if src in {"SUMMARY", "SUMMARY_AI"} or et == "SUMMARY_AI":
                        det = ret.get("detail") if isinstance(ret.get("detail"), dict) else {}
                        if _allow(row, det, str(symbol), str(source)):
                            return None
            except Exception:
                logger.debug("[ENTRY ORDER LOW MOVE RANGE FALLBACK] check failed", exc_info=True)
            return ret
        patched._entry_order_low_move_range_fallback_v1 = True
        patched._original = orig
        eob._low_move_hard_block = patched
        _INSTALLED = True
        logger.warning("[ENTRY ORDER LOW MOVE RANGE FALLBACK] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[ENTRY ORDER LOW MOVE RANGE FALLBACK] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[ENTRY ORDER LOW MOVE RANGE FALLBACK] auto install failed")
