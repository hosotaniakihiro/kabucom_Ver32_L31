# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-1M-RANGE-RELAX"
_INSTALLED = False
_ORIGINAL = None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _first(row: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _source_is_summary_ai(source: Any, row: dict) -> bool:
    src = str(source or row.get("source") or row.get("entry_type") or row.get("pipeline_source") or "").strip().upper()
    text = " ".join(str(row.get(k) or "") for k in ("entry_type", "source", "reason", "ai_reason", "model_used")).upper()
    return src in {"SUMMARY_AI", "SUMMARY", "PUSH", "PUSH_SUMMARY"} or "SUMMARY_AI" in src or "SRC=SUMMARY" in text or "SUMMARY_AI" in text


def _is_1m(row: dict) -> bool:
    found = False
    for key in ("interval", "timeframe", "bar_interval", "summary_interval", "source_interval"):
        v = row.get(key)
        if v is None or str(v).strip() == "":
            continue
        found = True
        s = str(v).strip().lower().replace(" ", "")
        if s in {"1", "1m", "1min", "1minute", "1分", "1分足"}:
            return True
        if s in {"3", "3m", "3min", "5", "5m", "5min", "3分", "5分"}:
            return False
    return not found


def _range_values(row: dict) -> tuple[float, float, float]:
    close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
    rv = max(0.0, high - low) if high > 0 and low > 0 and high >= low else _safe_float(_first(row, ("range_value", "intraday_range_value"), 0.0), 0.0)
    rp = rv / close if close > 0 and rv > 0 else _safe_float(_first(row, ("range_pct", "intraday_range_pct"), 0.0), 0.0)
    if rp > 1.0:
        rp = rp / 100.0
    return close, rv, rp


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    os.environ.setdefault("SUMMARY_AI_1M_MIN_RANGE_PCT", "0.0003")
    os.environ.setdefault("SUMMARY_AI_1M_MIN_RANGE_VALUE", "1.0")
    try:
        from trading.handlers import entry_order_builder as eob
        cur = getattr(eob, "_low_move_hard_block", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI 1M RANGE RELAX] target not callable version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_1m_range_relax_v1", False):
            _INSTALLED = True
            return True
        _ORIGINAL = getattr(cur, "_original", cur)

        def _patched(entry_row: dict, *, symbol: str, source: str):
            result = _ORIGINAL(entry_row, symbol=symbol, source=source)
            try:
                if not (isinstance(result, dict) and result.get("ok") is False and result.get("reason") == "LOW_MOVE_RANGE_TOO_SMALL"):
                    return result
                row = entry_row if isinstance(entry_row, dict) else {}
                if not _source_is_summary_ai(source, row) or not _is_1m(row):
                    return result
                min_value = _safe_float(os.getenv("SUMMARY_AI_1M_MIN_RANGE_VALUE", "1.0"), 1.0)
                min_pct = _safe_float(os.getenv("SUMMARY_AI_1M_MIN_RANGE_PCT", "0.0003"), 0.0003)
                close, rv, rp = _range_values(row)
                if close <= 0 or rv < min_value or rp < min_pct:
                    return result
                old_min = getattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", None)
                try:
                    setattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", min_pct)
                    row["summary_ai_1m_range_relaxed"] = True
                    row["summary_ai_1m_min_range_pct"] = min_pct
                    row["summary_ai_1m_min_range_value"] = min_value
                    if rv > 0 and _safe_float(_first(row, ("atr_1m", "atr", "ATR"), 0.0), 0.0) <= 0:
                        row["atr"] = rv
                        row["atr_1m"] = rv
                        row["ATR"] = rv
                    retry = _ORIGINAL(row, symbol=symbol, source=source)
                    logger.warning(
                        "[SUMMARY AI 1M RANGE RELAX] retry symbol=%s source=%s range_value=%.4f range_pct=%.6f min_pct=%.6f ok=%s reason=%s version=%s",
                        symbol, source, rv, rp, min_pct, isinstance(retry, dict) and retry.get("ok"), retry.get("reason") if isinstance(retry, dict) else type(retry).__name__, VERSION,
                    )
                    return retry
                finally:
                    if old_min is not None:
                        setattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", old_min)
            except Exception:
                logger.exception("[SUMMARY AI 1M RANGE RELAX] retry failed symbol=%s source=%s version=%s", symbol, source, VERSION)
                return result

        _patched._summary_ai_1m_range_relax_v1 = True  # type: ignore[attr-defined]
        _patched._original = _ORIGINAL  # type: ignore[attr-defined]
        eob._low_move_hard_block = _patched
        _INSTALLED = True
        logger.warning("[SUMMARY AI 1M RANGE RELAX] installed version=%s min_pct=%s min_value=%s", VERSION, os.getenv("SUMMARY_AI_1M_MIN_RANGE_PCT"), os.getenv("SUMMARY_AI_1M_MIN_RANGE_VALUE"))
        return True
    except Exception:
        logger.exception("[SUMMARY AI 1M RANGE RELAX] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI 1M RANGE RELAX] auto install failed")


__all__ = ["VERSION", "install"]
