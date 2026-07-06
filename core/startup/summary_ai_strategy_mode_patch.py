# -*- coding: utf-8 -*-
"""
Summary-AI strategy mode classifier.

Adds explicit strategy separation to Summary-AI candidates:
- BREAKOUT: trend-following high/MA/VWAP continuation.
- PULLBACK: trend-following dip/reclaim inside an up/down trend.
- REVERSAL: counter-trend mean-reversion candidate. Default is conservative.

This patch does not loosen board, liquidity, ATR, low-volatility, or time guards.
It only tags rows and optionally blocks low-quality/disabled strategy modes before
order build.
"""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-STRATEGY-MODE-BREAKOUT-PULLBACK-REVERSAL"
_INSTALLED = False
_ORIG_BUILD_APPROVED_ROW = None
_ORIG_PASSES_QUALITY = None
_ORIG_SORT_KEY = None


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        if isinstance(v, str):
            s = v.strip().replace(",", "").replace("円", "").replace("株", "").replace("%", "")
            if s == "" or s.lower() in {"none", "nan", "null", "<na>", "pd.na", "-", "－", "—"}:
                return float(default)
            v = s
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _s(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _side(row: dict[str, Any]) -> str:
    s = _s(row.get("side") or row.get("ai_side") or row.get("entry_decision"))
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return "BUY"


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip() != "":
            return v
    raw = row.get("source_row") or row.get("ai_row") or row.get("_raw") or row.get("raw")
    if isinstance(raw, dict):
        for k in keys:
            v = raw.get(k)
            if v is not None and str(v).strip() != "":
                return v
    return default


def _num(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return _f(_first(row, tuple(keys), default), default)


def _boolish(row: dict[str, Any], *keys: str) -> bool:
    for k in keys:
        v = _first(row, (k,), None)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return float(v) != 0.0
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    return False


def _ma_state(row: dict[str, Any]) -> tuple[float, float, float]:
    ma5 = _num(row, "ma5", "ma_5", "MA5", "display_ma5")
    ma25 = _num(row, "ma25", "ma_25", "MA25", "display_ma25")
    ma75 = _num(row, "ma75", "ma_75", "MA75", "display_ma75")
    return ma5, ma25, ma75


def _trend_alignment(row: dict[str, Any], side: str) -> tuple[int, list[str]]:
    reasons: list[str] = []
    aligned = 0
    slopes = {
        "1m": _num(row, "slope_1m", "slope", "score_slope", "slope_atr_scaled"),
        "3m": _num(row, "slope_3m", "slope_3"),
        "5m": _num(row, "slope_5m", "slope_5"),
    }
    eps = _env_float("SUMMARY_AI_STRATEGY_SLOPE_EPS", 0.0)
    for tf, slope in slopes.items():
        if side == "BUY" and slope > eps:
            aligned += 1
            reasons.append(f"{tf}_slope_up")
        elif side == "SELL" and slope < -eps:
            aligned += 1
            reasons.append(f"{tf}_slope_down")
    return aligned, reasons


def _classify(row: dict[str, Any]) -> dict[str, Any]:
    side = _side(row)
    close = _num(row, "close_price", "price", "current_price", "close", "last_price")
    high = _num(row, "high", "high_price", "HighPrice", "day_high")
    low = _num(row, "low", "low_price", "LowPrice", "day_low")
    vwap = _num(row, "vwap", "VWAP", "display_vwap")
    rsi = _num(row, "rsi", "RSI", default=50.0)
    macd = _num(row, "macd", "MACD")
    signal = _num(row, "signal", "macd_signal", "MACDSignal")
    hist = _num(row, "hist", "macd_hist", "histogram")
    atr = _num(row, "atr", "ATR")
    range_pct = _num(row, "range_pct", "intrabar_range_pct", "_intrabar_range_pct")
    if range_pct <= 0 and close > 0 and high > low:
        range_pct = abs(high - low) / close
    ma5, ma25, ma75 = _ma_state(row)
    above_ma5 = close > 0 and ma5 > 0 and close >= ma5
    below_ma5 = close > 0 and ma5 > 0 and close <= ma5
    above_vwap = close > 0 and vwap > 0 and close >= vwap
    below_vwap = close > 0 and vwap > 0 and close <= vwap
    high_break = _boolish(row, "above_vwap_recent", "golden_cross_recent", "golden_cross_continuation")
    low_break = _boolish(row, "below_vwap_recent", "dead_cross_recent", "dead_cross_continuation")
    bullish_stack = _boolish(row, "ma_stack_bullish") or (ma5 > 0 and ma25 > 0 and ma75 > 0 and ma5 >= ma25 >= ma75)
    bearish_stack = _boolish(row, "ma_stack_bearish") or (ma5 > 0 and ma25 > 0 and ma75 > 0 and ma5 <= ma25 <= ma75)
    aligned, slope_reasons = _trend_alignment(row, side)

    reasons: list[str] = []
    breakout = 0.0
    pullback = 0.0
    reversal = 0.0

    if side == "BUY":
        if above_ma5:
            breakout += 1.0; reasons.append("price_above_ma5")
        if above_vwap:
            breakout += 1.0; reasons.append("price_above_vwap")
        if bullish_stack:
            breakout += 1.0; reasons.append("ma_stack_bullish")
        if hist > 0 or macd >= signal:
            breakout += 0.5; reasons.append("macd_not_bad")
        if high_break or (high > 0 and close >= high * 0.995):
            breakout += 0.7; reasons.append("near_high_break")
        breakout += min(1.5, aligned * 0.5)
        # Pullback is still trend-following: larger trend OK, short-term dip/reclaim near MA/VWAP.
        if bullish_stack or aligned >= 1:
            pullback += 1.0; reasons.append("larger_trend_buy")
        if ma5 > 0 and close > 0 and abs(close - ma5) / close <= _env_float("SUMMARY_AI_PULLBACK_MA5_NEAR_PCT", 0.004):
            pullback += 1.0; reasons.append("near_ma5")
        if vwap > 0 and close > 0 and abs(close - vwap) / close <= _env_float("SUMMARY_AI_PULLBACK_VWAP_NEAR_PCT", 0.006):
            pullback += 0.8; reasons.append("near_vwap")
        if rsi <= _env_float("SUMMARY_AI_PULLBACK_BUY_RSI_MAX", 58.0):
            pullback += 0.4; reasons.append("rsi_not_overheated")
        if above_ma5 or above_vwap:
            pullback += 0.5; reasons.append("reclaim_short_average")
        if rsi <= _env_float("SUMMARY_AI_REVERSAL_BUY_RSI", 35.0):
            reversal += 1.2; reasons.append("rsi_oversold")
        if below_vwap:
            reversal += 0.6; reasons.append("below_vwap_mean_revert")
        if below_ma5:
            reversal += 0.4; reasons.append("below_ma5_mean_revert")
    else:
        if below_ma5:
            breakout += 1.0; reasons.append("price_below_ma5")
        if below_vwap:
            breakout += 1.0; reasons.append("price_below_vwap")
        if bearish_stack:
            breakout += 1.0; reasons.append("ma_stack_bearish")
        if hist < 0 or macd <= signal:
            breakout += 0.5; reasons.append("macd_not_bad_sell")
        if low_break or (low > 0 and close <= low * 1.005):
            breakout += 0.7; reasons.append("near_low_break")
        breakout += min(1.5, aligned * 0.5)
        if bearish_stack or aligned >= 1:
            pullback += 1.0; reasons.append("larger_trend_sell")
        if ma5 > 0 and close > 0 and abs(close - ma5) / close <= _env_float("SUMMARY_AI_PULLBACK_MA5_NEAR_PCT", 0.004):
            pullback += 1.0; reasons.append("near_ma5")
        if vwap > 0 and close > 0 and abs(close - vwap) / close <= _env_float("SUMMARY_AI_PULLBACK_VWAP_NEAR_PCT", 0.006):
            pullback += 0.8; reasons.append("near_vwap")
        if rsi >= _env_float("SUMMARY_AI_PULLBACK_SELL_RSI_MIN", 42.0):
            pullback += 0.4; reasons.append("rsi_not_oversold")
        if below_ma5 or below_vwap:
            pullback += 0.5; reasons.append("reclaim_short_average_sell")
        if rsi >= _env_float("SUMMARY_AI_REVERSAL_SELL_RSI", 70.0):
            reversal += 1.2; reasons.append("rsi_overbought")
        if above_vwap:
            reversal += 0.6; reasons.append("above_vwap_mean_revert")
        if above_ma5:
            reversal += 0.4; reasons.append("above_ma5_mean_revert")

    # Penalize low-volatility pseudo signals, but do not replace existing low-vol guard.
    if range_pct > 0 and range_pct < _env_float("SUMMARY_AI_STRATEGY_MIN_RANGE_PCT_SOFT", 0.004):
        breakout -= 0.4
        pullback -= 0.2
        reasons.append("strategy_low_range_soft_penalty")
    if atr <= 0:
        reasons.append("atr_missing_strategy_only")
    reasons.extend(slope_reasons)

    scores = {"BREAKOUT": breakout, "PULLBACK": pullback, "REVERSAL": reversal}
    mode = max(scores, key=scores.get)
    mode_score = float(scores[mode])
    if mode == "REVERSAL" and not _env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
        # Default: do not actively prefer counter-trend reversal. Route it as WATCH_ONLY unless very strong.
        strong_min = _env_float("SUMMARY_AI_STRATEGY_REVERSAL_STRONG_MIN", 3.2)
        if mode_score < strong_min:
            mode = "WATCH_ONLY_REVERSAL"
    return {
        "strategy_mode": mode,
        "strategy_score": round(mode_score, 4),
        "strategy_breakout_score": round(breakout, 4),
        "strategy_pullback_score": round(pullback, 4),
        "strategy_reversal_score": round(reversal, 4),
        "strategy_reasons": "/".join(dict.fromkeys(reasons))[:500],
        "strategy_aligned_mtf_count": aligned,
        "strategy_range_pct": round(range_pct, 6),
    }


def _enrich(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return row
    try:
        tags = _classify(row)
        row.update(tags)
        # Keep reason human-readable without losing original AI reason.
        base = str(row.get("reason") or row.get("ai_reason") or "")
        add = f"strategy={tags['strategy_mode']} score={tags['strategy_score']} reasons={tags['strategy_reasons']}"
        if add not in base:
            row["strategy_reason"] = add
            row["reason"] = (base + " | " + add).strip(" |")
    except Exception:
        logger.debug("[SUMMARY AI STRATEGY MODE] enrich failed row=%s", row, exc_info=True)
    return row


def _strategy_enabled(row: dict[str, Any]) -> tuple[bool, str]:
    if not _env_bool("SUMMARY_AI_STRATEGY_MODE_GUARD_ENABLED", True):
        return True, "disabled"
    mode = str(row.get("strategy_mode") or "").strip().upper()
    score = _f(row.get("strategy_score"), 0.0)
    if mode == "BREAKOUT":
        return score >= _env_float("SUMMARY_AI_STRATEGY_BREAKOUT_MIN", 2.0), "breakout_score_low"
    if mode == "PULLBACK":
        return score >= _env_float("SUMMARY_AI_STRATEGY_PULLBACK_MIN", 2.0), "pullback_score_low"
    if mode == "REVERSAL":
        if not _env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
            return False, "reversal_disabled"
        return score >= _env_float("SUMMARY_AI_STRATEGY_REVERSAL_MIN", 3.2), "reversal_score_low"
    if mode == "WATCH_ONLY_REVERSAL":
        return False, "watch_only_reversal"
    # Unknown mode should not fail-open into order entry.
    return False, "strategy_unknown"


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD_APPROVED_ROW, _ORIG_PASSES_QUALITY, _ORIG_SORT_KEY
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_STRATEGY_MODE_ENABLED", True):
        logger.warning("[SUMMARY AI STRATEGY MODE] disabled by env")
        return False
    try:
        import trading.entry.summary_ai.executor as ex

        cur_build = getattr(ex, "build_approved_row", None)
        cur_quality = getattr(ex, "_passes_strict_candidate_quality", None)
        cur_sort = getattr(ex, "_sort_key", None)
        if not callable(cur_build):
            logger.warning("[SUMMARY AI STRATEGY MODE] target build_approved_row missing version=%s", VERSION)
            return False

        if not getattr(cur_build, "_summary_ai_strategy_mode_v1", False):
            _ORIG_BUILD_APPROVED_ROW = cur_build
            @wraps(cur_build)
            def _build_approved_row_with_strategy(ai_ok_item: dict[str, Any]):
                row = cur_build(ai_ok_item)
                if isinstance(row, dict):
                    _enrich(row)
                    logger.warning(
                        "[SUMMARY AI STRATEGY MODE] approved symbol=%s side=%s mode=%s score=%s breakout=%s pullback=%s reversal=%s reasons=%s version=%s",
                        row.get("symbol"), row.get("side"), row.get("strategy_mode"), row.get("strategy_score"),
                        row.get("strategy_breakout_score"), row.get("strategy_pullback_score"), row.get("strategy_reversal_score"),
                        row.get("strategy_reasons"), VERSION,
                    )
                return row
            _build_approved_row_with_strategy._summary_ai_strategy_mode_v1 = True  # type: ignore[attr-defined]
            _build_approved_row_with_strategy._original = cur_build  # type: ignore[attr-defined]
            ex.build_approved_row = _build_approved_row_with_strategy

        if callable(cur_quality) and not getattr(cur_quality, "_summary_ai_strategy_quality_v1", False):
            _ORIG_PASSES_QUALITY = cur_quality
            @wraps(cur_quality)
            def _passes_quality_with_strategy(item: dict[str, Any]):
                ok, detail = cur_quality(item)
                if not ok:
                    return ok, detail
                if not isinstance(item, dict):
                    return ok, detail
                row = dict(item)
                try:
                    src = item.get("source_row")
                    ai = item.get("ai_row")
                    if isinstance(src, dict):
                        row.update({k: v for k, v in src.items() if k not in row})
                    if isinstance(ai, dict):
                        row.update({k: v for k, v in ai.items() if k not in row})
                except Exception:
                    pass
                _enrich(row)
                allow, reason = _strategy_enabled(row)
                if not allow:
                    return False, {
                        "symbol": row.get("symbol") or item.get("symbol"),
                        "side": row.get("side") or item.get("side"),
                        "reason": reason,
                        "strategy_mode": row.get("strategy_mode"),
                        "strategy_score": row.get("strategy_score"),
                        "strategy_reasons": row.get("strategy_reasons"),
                    }
                try:
                    item.update({k: row.get(k) for k in (
                        "strategy_mode", "strategy_score", "strategy_breakout_score", "strategy_pullback_score",
                        "strategy_reversal_score", "strategy_reasons", "strategy_aligned_mtf_count", "strategy_range_pct",
                    )})
                except Exception:
                    pass
                return True, detail
            _passes_quality_with_strategy._summary_ai_strategy_quality_v1 = True  # type: ignore[attr-defined]
            _passes_quality_with_strategy._original = cur_quality  # type: ignore[attr-defined]
            ex._passes_strict_candidate_quality = _passes_quality_with_strategy

        if callable(cur_sort) and not getattr(cur_sort, "_summary_ai_strategy_sort_v1", False):
            _ORIG_SORT_KEY = cur_sort
            @wraps(cur_sort)
            def _sort_key_with_strategy(item: dict[str, Any]):
                base = cur_sort(item)
                row = dict(item or {})
                try:
                    if isinstance(item.get("source_row"), dict):
                        row.update({k: v for k, v in item.get("source_row").items() if k not in row})
                    if isinstance(item.get("ai_row"), dict):
                        row.update({k: v for k, v in item.get("ai_row").items() if k not in row})
                    _enrich(row)
                    mode = str(row.get("strategy_mode") or "").upper()
                    mode_rank = {"BREAKOUT": 3, "PULLBACK": 2, "REVERSAL": 1, "WATCH_ONLY_REVERSAL": -1}.get(mode, 0)
                    strat_score = _f(row.get("strategy_score"), 0.0)
                    if isinstance(base, tuple):
                        return tuple(list(base) + [mode_rank, strat_score])
                    return (base, mode_rank, strat_score)
                except Exception:
                    return base
            _sort_key_with_strategy._summary_ai_strategy_sort_v1 = True  # type: ignore[attr-defined]
            _sort_key_with_strategy._original = cur_sort  # type: ignore[attr-defined]
            ex._sort_key = _sort_key_with_strategy

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI STRATEGY MODE] installed version=%s breakout_min=%s pullback_min=%s reversal_enabled=%s reversal_min=%s guard=%s",
            VERSION,
            os.getenv("SUMMARY_AI_STRATEGY_BREAKOUT_MIN", "2.0"),
            os.getenv("SUMMARY_AI_STRATEGY_PULLBACK_MIN", "2.0"),
            _env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False),
            os.getenv("SUMMARY_AI_STRATEGY_REVERSAL_MIN", "3.2"),
            _env_bool("SUMMARY_AI_STRATEGY_MODE_GUARD_ENABLED", True),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI STRATEGY MODE] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI STRATEGY MODE] auto install failed")


__all__ = ["VERSION", "install"]
