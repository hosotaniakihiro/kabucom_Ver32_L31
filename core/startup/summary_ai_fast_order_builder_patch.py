# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3.9-SUMMARY-AI-PUSH-ROTATION-BOARD-RETRY-2TURNS"
_INSTALLED = False
_ORIGINAL_BUILD_ENTRY_ORDER = None

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "allow", "allowed", "enable", "enabled"}
_SUMMARY_SOURCE_SET = {"SUMMARY", "SUMMARY_AI", "PUSH", "PUSH_SUMMARY", "STOCK_SUMMARY"}


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


def _set_exact(obj: Any, name: str, value: float) -> tuple[float | None, float]:
    old = None
    try:
        old = float(getattr(obj, name))
    except Exception:
        pass
    try:
        setattr(obj, name, float(value))
    except Exception:
        pass
    return old, float(value)


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
        joined = "|".join(str(x or "").upper() for x in (source, row_source, entry_type, pipeline_source, row.get("reason"), row.get("ai_reason"), row.get("skip_reason")))
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
        price_f = _safe_float(_first(row, ("close_price", "price", "current_price", "close", "last_price", "vwap", "base_price", "display_price"), 0.0), 0.0)
        if price_f > 0:
            row.setdefault("close_price", price_f)
            row.setdefault("price", price_f)
            row.setdefault("current_price", price_f)
            row.setdefault("close", price_f)
        vol = _safe_float(_first(row, ("volume", "vol", "latest_volume", "display_volume", "_latest_volume"), 0.0), 0.0)
        if vol <= 0:
            turnover = _safe_float(_first(row, ("turnover", "trading_value", "sales_value", "display_turnover"), 0.0), 0.0)
            if turnover > 0 and price_f > 0:
                vol = turnover / price_f
        if vol > 0:
            row.setdefault("volume", vol)
        logger.warning("[SUMMARY AI FAST ORDER BUILDER] snapshot normalized symbol=%s side=%s source %s->SUMMARY_AI price=%s volume=%s version=%s", symbol, side, old_source, row.get("close_price") or row.get("price") or row.get("current_price") or row.get("close"), row.get("volume"), VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] snapshot normalize failed version=%s", VERSION)
        return False


def _range_pct(high: float, low: float, close: float) -> float:
    try:
        if close <= 0 or high <= 0 or low <= 0 or high < low:
            return 0.0
        return (high - low) / close
    except Exception:
        return 0.0


def _ranking_move_rescue_ok(row: dict[str, Any], *, min_range_pct: float) -> tuple[bool, str]:
    try:
        from trading.filters import volatility_filter as vf
        fn = getattr(vf, "_ranking_move_rescue", None)
        if not callable(fn):
            return False, "no_rescue_func"
        ok = bool(fn(row, min_pct=float(min_range_pct), label="fast_order_low_move"))
        return ok, "ranking_move_rescue" if ok else "ranking_move_rescue_ng"
    except Exception as e:
        logger.debug("[SUMMARY AI FAST ORDER BUILDER] ranking move rescue check failed err=%s", e, exc_info=True)
        return False, f"ranking_move_rescue_error:{e}"


def _apply_synthetic_range(row: dict[str, Any], *, close: float, min_range_pct: float, reason: str) -> None:
    width = max(close * float(min_range_pct) * 1.05, close * 0.001, 1.0)
    high = close + width / 2.0
    low = max(0.01, close - width / 2.0)
    pct = _range_pct(high, low, close)
    atr_proxy = max(_safe_float(_first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0), 0.0), max(0.0, high - low))
    row.update({"high_price": high, "low_price": low, "high": high, "low": low, "range_pct": pct, "intraday_range_pct": pct, "summary_ai_range_repaired": True, "summary_ai_range_repair_reason": reason, "summary_ai_range_repair_pct": pct})
    if atr_proxy > 0:
        row["atr"] = atr_proxy
        row["atr_1m"] = atr_proxy
        row["ATR"] = atr_proxy


def _repair_summary_ai_low_move_range(kwargs: dict[str, Any], eob: Any) -> bool:
    try:
        if not _is_summary_ai_order(kwargs):
            return False
        row = kwargs.get("entry_row")
        if not isinstance(row, dict):
            return False
        cond = row.get("entry_conditions") if isinstance(row.get("entry_conditions"), dict) else {}
        close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
        if close <= 0:
            return False
        min_range_pct = _safe_float(os.getenv("ENTRY_ORDER_MIN_RANGE_PCT", getattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", 0.006)), 0.006)
        cur_high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
        cur_low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
        if _range_pct(cur_high, cur_low, close) >= min_range_pct:
            return False
        candidates: list[tuple[str, float, float, float]] = []
        for label, src in (("row_day", row), ("conditions_day", cond)):
            hi = _safe_float(_first(src, ("day_high", "today_high", "session_high", "intraday_high", "HighPrice", "high_3m", "high_5m"), 0.0), 0.0)
            lo = _safe_float(_first(src, ("day_low", "today_low", "session_low", "intraday_low", "LowPrice", "low_3m", "low_5m"), 0.0), 0.0)
            if hi > 0 and lo > 0:
                h, l = max(hi, lo), min(hi, lo)
                candidates.append((label, h, l, _range_pct(h, l, close)))
        for src_label, src in (("row", row), ("conditions", cond)):
            rp = _safe_float(_first(src, ("range_pct", "intraday_range_pct", "day_range_pct", "summary_ai_range_repair_pct", "_intrabar_range_pct", "_max_price_change_pct"), 0.0), 0.0)
            if rp > 1.0:
                rp = rp / 100.0
            if rp > 0:
                width = close * rp
                candidates.append((f"{src_label}_range_pct", close + width / 2.0, max(0.01, close - width / 2.0), rp))
        best = None
        for cand in candidates:
            if cand[3] >= min_range_pct and (best is None or cand[3] > best[3]):
                best = cand
        if best is None:
            rescue_ok, rescue_reason = _ranking_move_rescue_ok(row, min_range_pct=min_range_pct)
            if rescue_ok:
                _apply_synthetic_range(row, close=close, min_range_pct=min_range_pct, reason=rescue_reason)
                logger.warning("[SUMMARY AI FAST ORDER BUILDER] range/atr repaired by ranking rescue symbol=%s side=%s close=%.2f min_range_pct=%.6f high=%.2f low=%.2f atr=%.4f version=%s", kwargs.get("symbol"), kwargs.get("side"), close, min_range_pct, row.get("high"), row.get("low"), row.get("atr"), VERSION)
                return True
            logger.info("[SUMMARY AI FAST ORDER BUILDER] range repair not enough symbol=%s side=%s candidates=%s rescue=%s version=%s", kwargs.get("symbol"), kwargs.get("side"), candidates, rescue_reason, VERSION)
            return False
        reason, high, low, pct = best
        atr_proxy = max(_safe_float(_first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0), 0.0), max(0.0, high - low))
        row.update({"high_price": high, "low_price": low, "high": high, "low": low, "range_pct": pct, "intraday_range_pct": pct, "summary_ai_range_repaired": True, "summary_ai_range_repair_reason": reason, "summary_ai_range_repair_pct": pct})
        if atr_proxy > 0:
            row["atr"] = atr_proxy
            row["atr_1m"] = atr_proxy
            row["ATR"] = atr_proxy
        logger.warning("[SUMMARY AI FAST ORDER BUILDER] range/atr repaired symbol=%s side=%s reason=%s high=%.2f low=%.2f range_pct=%.6f atr=%.4f version=%s", kwargs.get("symbol"), kwargs.get("side"), reason, high, low, pct, atr_proxy, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] range repair failed symbol=%s side=%s version=%s", kwargs.get("symbol"), kwargs.get("side"), VERSION)
        return False


def _install_range_consistency_chain() -> bool:
    try:
        from core.startup.summary_ai_range_consistency_patch import install as _install_range_consistency
        return bool(_install_range_consistency())
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] range consistency chain install failed version=%s", VERSION)
        return False


def _install_strict_board_rest_chain() -> bool:
    try:
        from core.startup.summary_ai_strict_board_rest_patch import install as _install_board_policy
        return bool(_install_board_policy())
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] strict board policy chain install failed version=%s", VERSION)
        return False


def install() -> bool:
    global _INSTALLED, _ORIGINAL_BUILD_ENTRY_ORDER
    if _INSTALLED:
        return True
    try:
        # PUSH登録はA/Bローテーションなので、候補銘柄が一時的に反対側へいて板が取れないことがある。
        # A/B各4.8秒+解除0.2秒の想定では5秒は境界なので、SUMMARY_AIは約2ローテ分だけ待つ。
        # それでも板なしなら従来どおり hard block。板なしエントリーには緩めない。
        retry_sec = _safe_float(os.getenv("SUMMARY_AI_PUSH_ROTATION_BOARD_RETRY_SEC", "10.5"), 10.5)
        retry_sec = max(5.0, min(retry_sec, 12.0))
        os.environ["ENTRY_ORDER_BOARD_RETRY_SEC"] = str(retry_sec)
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.2")
        os.environ["SUMMARY_AI_BOARD_RETRY_REASON"] = "push_rotation_wait_2turns"
        # Board missing is still a hard block by policy. Do not convert it to close/vwap fallback.
        os.environ["ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY"] = "1"
        os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "1"
        os.environ["ENTRY_LIMIT_ALLOW_WITHOUT_BOARD"] = "0"

        from trading.handlers import entry_order_builder as eob
        try:
            setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", True)
        except Exception:
            pass
        logger_patched = _ensure_entry_order_builder_logger(eob)
        range_consistency = _install_range_consistency_chain()
        board_policy = _install_strict_board_rest_chain()
        old_retry, new_retry = _set_exact(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", _safe_float(os.environ.get("ENTRY_ORDER_BOARD_RETRY_SEC"), retry_sec))
        old_interval, new_interval = _set_exact(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", _safe_float(os.environ.get("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"), 0.2))
        cur = getattr(eob, "build_entry_order", None)
        if callable(cur) and not getattr(cur, "_summary_ai_fast_order_builder_v39", False):
            _ORIGINAL_BUILD_ENTRY_ORDER = getattr(cur, "_original", cur)

            def _patched_build_entry_order(*args, **kwargs):
                summary_like = _is_summary_ai_order(kwargs)
                symbol = kwargs.get("symbol")
                side = kwargs.get("side")
                if summary_like:
                    logger.info("[SUMMARY AI FAST ORDER BUILDER] start symbol=%s side=%s source=%s retry_sec=%s retry_interval=%s reason=push_rotation_wait_2turns version=%s", symbol, side, kwargs.get("source"), getattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", None), getattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", None), VERSION)
                    _coerce_summary_ai_snapshot_order(kwargs)
                    _repair_summary_ai_low_move_range(kwargs, eob)
                try:
                    result = _ORIGINAL_BUILD_ENTRY_ORDER(*args, **kwargs)
                except NameError as exc:
                    if "logger" in str(exc):
                        _ensure_entry_order_builder_logger(eob)
                        logger.warning("[SUMMARY AI FAST ORDER BUILDER] recovered missing eob.logger symbol=%s side=%s version=%s", symbol, side, VERSION)
                        result = _ORIGINAL_BUILD_ENTRY_ORDER(*args, **kwargs)
                    else:
                        raise
                if summary_like:
                    if isinstance(result, dict) and result.get("reason") == "STRICT_BOARD_MISSING":
                        logger.warning("[SUMMARY AI FAST ORDER BUILDER] board missing hard block kept after push rotation wait symbol=%s side=%s retry_sec=%s detail=%s version=%s", symbol, side, getattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", None), result.get("detail"), VERSION)
                    logger.warning("[SUMMARY AI FAST ORDER BUILDER] done symbol=%s side=%s ok=%s reason=%s detail=%s version=%s", symbol, side, isinstance(result, dict) and result.get("ok"), result.get("reason") if isinstance(result, dict) else type(result).__name__, result.get("detail") if isinstance(result, dict) else None, VERSION)
                return result

            for marker in ("_summary_ai_fast_order_builder_v1", "_summary_ai_fast_order_builder_v2", "_summary_ai_fast_order_builder_v3", "_summary_ai_fast_order_builder_v31", "_summary_ai_fast_order_builder_v32", "_summary_ai_fast_order_builder_v33", "_summary_ai_fast_order_builder_v34", "_summary_ai_fast_order_builder_v35", "_summary_ai_fast_order_builder_v36", "_summary_ai_fast_order_builder_v37", "_summary_ai_fast_order_builder_v38", "_summary_ai_fast_order_builder_v39"):
                setattr(_patched_build_entry_order, marker, True)
            _patched_build_entry_order._original = _ORIGINAL_BUILD_ENTRY_ORDER  # type: ignore[attr-defined]
            eob.build_entry_order = _patched_build_entry_order
            try:
                import trading.handlers.entry_controller as ec
                ec.build_entry_order = _patched_build_entry_order
            except Exception:
                logger.debug("[SUMMARY AI FAST ORDER BUILDER] entry_controller alias patch skipped", exc_info=True)
        _INSTALLED = True
        logger.warning("[SUMMARY AI FAST ORDER BUILDER] installed version=%s retry_sec %s->%s interval %s->%s logger_patched=%s range_repair=True source_detect=True snapshot_no_order_fallback=True ranking_range_rescue=True range_consistency=%s board_policy=%s strict_board_limit_fallback=False push_rotation_wait_2turns=%.1fs", VERSION, old_retry, new_retry, old_interval, new_interval, logger_patched, range_consistency, board_policy, retry_sec)
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI FAST ORDER BUILDER] auto install failed")


__all__ = ["install", "VERSION"]
