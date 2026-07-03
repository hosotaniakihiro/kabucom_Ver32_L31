# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_fast_order_builder_patch.py
# Version: V3.2-SUMMARY-AI-SNAPSHOT-NO-ORDER-FALLBACK
# ------------------------------------------------------------
# SUMMARY_AI が AI_OK/pending 登録まで進んだ後、entry_controller 側で
# source/entry_type が SUMMARY に寄って注文ビルダーが SUMMARY_AI ルートへ
# 入らず、snapshot_no_order / entry_controller_no_order で止まる症状を補修する。
#
# 方針:
#   - ENTRY_ORDER_MIN_RANGE_PCT などの厳格条件は緩めない。
#   - SUMMARY/SUMMARY_AI/PUSH 由来で AI_OK 済みの候補だけ、注文ビルダー呼び出し
#     直前に source=SUMMARY_AI / entry_type=SUMMARY_AI へ正規化する。
#   - ランキング/殿様は従来通り 5秒足 breakout ルートを維持する。
#   - 板が無い場合でも、既存 row の close/price/current_price/vwap から
#     SUMMARY_AI の安全な LIMIT fallback を使えるようにする。
#   - 1分足 high/low が潰れている場合だけ、既存 row 情報からレンジを補修する。
# ============================================================
from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3.2-SUMMARY-AI-SNAPSHOT-NO-ORDER-FALLBACK"
_INSTALLED = False
_ORIGINAL_BUILD_ENTRY_ORDER = None


_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "allow", "allowed", "enable", "enabled"}
_SUMMARY_SOURCE_SET = {"SUMMARY", "SUMMARY_AI", "PUSH", "PUSH_SUMMARY", "STOCK_SUMMARY"}


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


def _truthy(v: Any) -> bool:
    try:
        if isinstance(v, bool):
            return v
        return str(v or "").strip().lower() in _TRUE_SET
    except Exception:
        return False


def _row_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    row = kwargs.get("entry_row")
    if isinstance(row, dict):
        return row
    row = {}
    kwargs["entry_row"] = row
    return row


def _is_summary_ai_order(kwargs: dict[str, Any]) -> bool:
    try:
        source = str(kwargs.get("source") or "").strip().upper()
        row = kwargs.get("entry_row") if isinstance(kwargs.get("entry_row"), dict) else {}
        row_source = str(row.get("source") or "").strip().upper()
        entry_type = str(row.get("entry_type") or kwargs.get("entry_type") or "").strip().upper()
        pipeline_source = str(row.get("pipeline_source") or "").strip().upper()
        joined = "|".join(
            str(x or "").upper()
            for x in (
                source,
                row_source,
                entry_type,
                pipeline_source,
                row.get("reason"),
                row.get("ai_reason"),
                row.get("skip_reason"),
            )
        )
        if source in _SUMMARY_SOURCE_SET or row_source in _SUMMARY_SOURCE_SET or pipeline_source == "SUMMARY":
            return True
        if "SUMMARY_AI" in joined or "SRC=SUMMARY" in joined:
            return True
        if _truthy(row.get("ai_gate_allow")) or _truthy(row.get("preapproved")) or _truthy(row.get("summary_ai_ok")):
            return True
        return False
    except Exception:
        return False


def _coerce_summary_ai_snapshot_order(kwargs: dict[str, Any]) -> bool:
    """Make SUMMARY/SUMMARY_AI candidates use the SUMMARY_AI order-builder route."""
    try:
        if not _is_summary_ai_order(kwargs):
            return False
        row = _row_from_kwargs(kwargs)
        symbol = kwargs.get("symbol") or row.get("symbol")
        side = str(kwargs.get("side") or row.get("side") or row.get("entry_decision") or row.get("ai_side") or "").strip().upper()
        old_source = str(kwargs.get("source") or row.get("source") or "").strip().upper()

        if old_source and old_source != "SUMMARY_AI":
            row.setdefault("original_source", old_source)
        kwargs["source"] = "SUMMARY_AI"
        row["source"] = "SUMMARY_AI"
        row["entry_type"] = "SUMMARY_AI"
        if side in {"BUY", "SELL"}:
            kwargs["side"] = side
            row.setdefault("side", side)
            row.setdefault("entry_decision", side)
            row.setdefault("ai_side", side)

        price = _first(
            row,
            (
                "close_price",
                "price",
                "current_price",
                "close",
                "last_price",
                "vwap",
                "base_price",
                "display_price",
            ),
            None,
        )
        price_f = _safe_float(price, 0.0)
        if price_f > 0:
            row.setdefault("close_price", price_f)
            row.setdefault("price", price_f)
            row.setdefault("current_price", price_f)
            row.setdefault("close", price_f)

        # Keep order-builder liquidity guard effective even when turnover exists but volume alias is missing.
        vol = _safe_float(_first(row, ("volume", "vol", "latest_volume", "display_volume", "_latest_volume"), 0.0), 0.0)
        if vol <= 0:
            turnover = _safe_float(_first(row, ("turnover", "trading_value", "sales_value", "display_turnover"), 0.0), 0.0)
            if turnover > 0 and price_f > 0:
                vol = turnover / price_f
        if vol > 0:
            row.setdefault("volume", vol)

        logger.warning(
            "[SUMMARY AI FAST ORDER BUILDER] snapshot order fallback normalized symbol=%s side=%s source %s->SUMMARY_AI price=%s volume=%s version=%s",
            symbol,
            side,
            old_source,
            row.get("close_price") or row.get("price") or row.get("current_price") or row.get("close"),
            row.get("volume"),
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] snapshot order fallback normalize failed kwargs_keys=%s version=%s", list(kwargs.keys()), VERSION)
        return False


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
        if not _is_summary_ai_order(kwargs):
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
            "display_high", "calc_high", "source_high",
        )
        low_names = (
            "day_low", "today_low", "session_low", "low_day", "low_today",
            "summary_low_day", "summary_day_low", "push_day_low", "latest_day_low",
            "min_price", "lowest_price", "LowPrice", "low_3m", "low_5m",
            "display_low", "calc_low", "source_low",
        )
        open_names = ("open_price", "open", "Open", "day_open", "today_open", "session_open")

        candidates: list[tuple[str, float, float, float]] = []
        alt_high = _safe_float(_first(row, high_names, 0.0), 0.0)
        alt_low = _safe_float(_first(row, low_names, 0.0), 0.0)
        if alt_high > 0 and alt_low > 0:
            h, l = max(alt_high, alt_low), min(alt_high, alt_low)
            candidates.append(("day_high_low", h, l, _range_pct(h, l, close)))

        op = _safe_float(_first(row, open_names, 0.0), 0.0)
        if op > 0:
            h = max(op, close, cur_high if cur_high > 0 else close)
            l = min(op, close, cur_low if cur_low > 0 else close)
            candidates.append(("open_close", h, l, _range_pct(h, l, close)))

        for name in ("range_value", "day_range_value", "price_range", "intraday_range"):
            rv = _safe_float(row.get(name), 0.0)
            if rv > 0:
                h = max(cur_high, close + rv / 2.0, close)
                l = min(cur_low if cur_low > 0 else close, close - rv / 2.0, close)
                candidates.append((name, h, l, _range_pct(h, l, close)))

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
        if callable(cur) and not getattr(cur, "_summary_ai_fast_order_builder_v32", False):
            _ORIGINAL_BUILD_ENTRY_ORDER = getattr(cur, "_original", cur)

            def _patched_build_entry_order(*args, **kwargs):
                summary_like = _is_summary_ai_order(kwargs)
                symbol = kwargs.get("symbol")
                side = kwargs.get("side")
                if summary_like:
                    logger.info(
                        "[SUMMARY AI FAST ORDER BUILDER] start symbol=%s side=%s source=%s retry_sec=%s retry_interval=%s version=%s",
                        symbol, side, kwargs.get("source"), getattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", None), getattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", None), VERSION,
                    )
                    _coerce_summary_ai_snapshot_order(kwargs)
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
                if summary_like:
                    logger.warning(
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
            _patched_build_entry_order._summary_ai_fast_order_builder_v31 = True  # type: ignore[attr-defined]
            _patched_build_entry_order._summary_ai_fast_order_builder_v32 = True  # type: ignore[attr-defined]
            _patched_build_entry_order._original = _ORIGINAL_BUILD_ENTRY_ORDER  # type: ignore[attr-defined]
            eob.build_entry_order = _patched_build_entry_order

            try:
                import trading.handlers.entry_controller as ec
                ec.build_entry_order = _patched_build_entry_order
            except Exception:
                logger.debug("[SUMMARY AI FAST ORDER BUILDER] entry_controller alias patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI FAST ORDER BUILDER] installed version=%s retry_sec %s->%s interval %s->%s logger_patched=%s range_repair=True source_detect=True snapshot_no_order_fallback=True",
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
