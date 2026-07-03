# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_fast_order_builder_patch.py
# Version: V3-SUMMARY-AI-RANGE-REPAIR-BEFORE-ORDER-BUILD
# ------------------------------------------------------------
# SUMMARY_AI が AI_OK → qty算出まで進んだあと、発注直前で
# LOW_MOVE_RANGE_TOO_SMALL になるケースを、閾値を緩めずに補修する。
#
# 方針:
#   - ENTRY_ORDER_MIN_RANGE_PCT は緩和しない。
#   - 1分足の high/low が close 付近に潰れている時だけ、entry_row 内の
#     day_high/day_low, session_high/session_low, open/current など既存情報から
#     実レンジを再構成する。
#   - 補完レンジが閾値以上の時だけ high_price/low_price を差し替える。
#   - 補完できなければ従来通り LOW_MOVE_RANGE_TOO_SMALL で止める。
#   - board retry は短縮し、logger 未定義も補正する。
# ============================================================
from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-SUMMARY-AI-RANGE-REPAIR-BEFORE-ORDER-BUILD"
_INSTALLED = False
_ORIGINAL_BUILD_ENTRY_ORDER = None


def _safe_float(v: Any, default: float) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _set_cap(obj: Any, name: str, cap: float) -> tuple[float | None, float]:
    old = None
    try:
        old = float(getattr(obj, name))
    except Exception:
        pass
    new = min(old if old is not None else cap, cap)
    try:
        setattr(obj, name, new)
    except Exception:
        pass
    return old, new


def _ensure_entry_order_builder_logger(eob: Any) -> bool:
    try:
        cur = getattr(eob, "logger", None)
        if cur is None or not hasattr(cur, "info") or not hasattr(cur, "warning"):
            eob.logger = logging.getLogger("trading.handlers.entry_order_builder")
            return True
        return False
    except Exception:
        return False


def _first(row: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        try:
            v = row.get(name)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _range_pct(high: float, low: float, close: float) -> float:
    try:
        if close <= 0 or high <= 0 or low <= 0 or high < low:
            return 0.0
        return (high - low) / close
    except Exception:
        return 0.0


def _repair_summary_ai_low_move_range(kwargs: dict[str, Any], eob: Any) -> bool:
    """Repair collapsed SUMMARY_AI high/low using only already-provided row data."""
    try:
        source = str(kwargs.get("source") or "").upper()
        if source != "SUMMARY_AI":
            return False
        row = kwargs.get("entry_row")
        if not isinstance(row, dict):
            return False

        close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
        if close <= 0:
            return False

        min_range_pct = _safe_float(
            os.getenv("ENTRY_ORDER_MIN_RANGE_PCT", getattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", 0.006)),
            0.006,
        )
        cur_high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
        cur_low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
        cur_range_pct = _range_pct(cur_high, cur_low, close)
        if cur_range_pct >= min_range_pct:
            return False

        high_names = (
            "day_high", "today_high", "session_high", "high_day", "high_today",
            "summary_high_day", "summary_day_high", "push_day_high", "latest_day_high",
            "max_price", "highest_price", "HighPrice", "high_3m", "high_5m",
        )
        low_names = (
            "day_low", "today_low", "session_low", "low_day", "low_today",
            "summary_low_day", "summary_day_low", "push_day_low", "latest_day_low",
            "min_price", "lowest_price", "LowPrice", "low_3m", "low_5m",
        )
        open_names = ("open_price", "open", "Open", "day_open", "today_open", "session_open")

        candidates: list[tuple[str, float, float, float]] = []
        alt_high = _safe_float(_first(row, high_names, 0.0), 0.0)
        alt_low = _safe_float(_first(row, low_names, 0.0), 0.0)
        if alt_high > 0 and alt_low > 0:
            candidates.append(("day_high_low", max(alt_high, alt_low), min(alt_high, alt_low), _range_pct(max(alt_high, alt_low), min(alt_high, alt_low), close)))

        op = _safe_float(_first(row, open_names, 0.0), 0.0)
        if op > 0:
            h = max(op, close, cur_high if cur_high > 0 else close)
            l = min(op, close, cur_low if cur_low > 0 else close)
            candidates.append(("open_close", h, l, _range_pct(h, l, close)))

        # If 3m/5m range is available but high/low columns were collapsed, use the widest strict candidate.
        best = None
        for cand in candidates:
            if cand[3] >= min_range_pct and (best is None or cand[3] > best[3]):
                best = cand
        if best is None:
            logger.info(
                "[SUMMARY AI FAST ORDER BUILDER] range repair not enough symbol=%s side=%s close=%.4f cur_high=%.4f cur_low=%.4f cur_range_pct=%.6f min_range_pct=%.6f candidates=%s version=%s",
                kwargs.get("symbol"), kwargs.get("side"), close, cur_high, cur_low, cur_range_pct, min_range_pct, candidates, VERSION,
            )
            return False

        reason, high, low, pct = best
        row["high_price"] = high
        row["low_price"] = low
        row["high"] = high
        row["low"] = low
        row["summary_ai_range_repaired"] = True
        row["summary_ai_range_repair_reason"] = reason
        row["summary_ai_range_repair_pct"] = pct
        logger.warning(
            "[SUMMARY AI FAST ORDER BUILDER] range repaired symbol=%s side=%s reason=%s close=%.4f old_high=%.4f old_low=%.4f new_high=%.4f new_low=%.4f range_pct=%.6f min_range_pct=%.6f version=%s",
            kwargs.get("symbol"), kwargs.get("side"), reason, close, cur_high, cur_low, high, low, pct, min_range_pct, VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] range repair failed symbol=%s side=%s version=%s", kwargs.get("symbol"), kwargs.get("side"), VERSION)
        return False


def install() -> bool:
    global _INSTALLED, _ORIGINAL_BUILD_ENTRY_ORDER
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_SEC", "0.8")
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.2")

        from trading.handlers import entry_order_builder as eob

        logger_patched = _ensure_entry_order_builder_logger(eob)
        old_retry, new_retry = _set_cap(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", _safe_float(os.environ.get("ENTRY_ORDER_BOARD_RETRY_SEC"), 0.8))
        old_interval, new_interval = _set_cap(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", _safe_float(os.environ.get("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"), 0.2))

        cur = getattr(eob, "build_entry_order", None)
        if callable(cur) and not getattr(cur, "_summary_ai_fast_order_builder_v3", False):
            _ORIGINAL_BUILD_ENTRY_ORDER = getattr(cur, "_original", cur)

            def _patched_build_entry_order(*args, **kwargs):
                source = str(kwargs.get("source") or "").upper()
                symbol = kwargs.get("symbol")
                side = kwargs.get("side")
                if source == "SUMMARY_AI":
                    logger.info(
                        "[SUMMARY AI FAST ORDER BUILDER] start symbol=%s side=%s retry_sec=%s retry_interval=%s version=%s",
                        symbol, side, getattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", None), getattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", None), VERSION,
                    )
                    _repair_summary_ai_low_move_range(kwargs, eob)
                try:
                    result = _ORIGINAL_BUILD_ENTRY_ORDER(*args, **kwargs)
                except NameError as exc:
                    if "logger" in str(exc):
                        _ensure_entry_order_builder_logger(eob)
                        logger.warning(
                            "[SUMMARY AI FAST ORDER BUILDER] recovered missing eob.logger symbol=%s side=%s version=%s",
                            symbol, side, VERSION,
                        )
                        result = _ORIGINAL_BUILD_ENTRY_ORDER(*args, **kwargs)
                    else:
                        raise
                if source == "SUMMARY_AI":
                    logger.info(
                        "[SUMMARY AI FAST ORDER BUILDER] done symbol=%s side=%s ok=%s reason=%s detail=%s version=%s",
                        symbol,
                        side,
                        isinstance(result, dict) and result.get("ok"),
                        result.get("reason") if isinstance(result, dict) else type(result).__name__,
                        result.get("detail") if isinstance(result, dict) else None,
                        VERSION,
                    )
                return result

            _patched_build_entry_order._summary_ai_fast_order_builder_v1 = True  # type: ignore[attr-defined]
            _patched_build_entry_order._summary_ai_fast_order_builder_v2 = True  # type: ignore[attr-defined]
            _patched_build_entry_order._summary_ai_fast_order_builder_v3 = True  # type: ignore[attr-defined]
            _patched_build_entry_order._original = _ORIGINAL_BUILD_ENTRY_ORDER  # type: ignore[attr-defined]
            eob.build_entry_order = _patched_build_entry_order

            try:
                import trading.handlers.entry_controller as ec
                ec.build_entry_order = _patched_build_entry_order
            except Exception:
                logger.debug("[SUMMARY AI FAST ORDER BUILDER] entry_controller alias patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI FAST ORDER BUILDER] installed version=%s retry_sec %s->%s interval %s->%s logger_patched=%s range_repair=True",
            VERSION,
            old_retry,
            new_retry,
            old_interval,
            new_interval,
            logger_patched,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI FAST ORDER BUILDER] auto install failed")


__all__ = ["install", "VERSION"]
