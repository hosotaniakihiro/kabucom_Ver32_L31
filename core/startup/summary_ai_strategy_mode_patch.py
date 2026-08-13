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


def _classify_core(row: dict[str, Any]) -> dict[str, Any]:
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


def _strategy_enabled_core(row: dict[str, Any]) -> tuple[bool, str]:
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

        # build_approved_row のラップは撤去した。
        # 旧 summary_ai_approved_row_range_repair_patch が install 時に
        # `getattr(cur, "_original", cur)` で素の実装を掴み直すため、
        # install順 (strategy_mode -> range_repair) によりこのラッパは
        # 実運用で必ず捨てられていた (合成順シミュレーションで確認済み)。
        # range_repair 側は trading/entry/summary_ai/executor.py へ本文化済み。

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


# ============================================================
# 戦略オーバーレイ 2 段
# (旧 core/startup/summary_ai_fibonacci_ev_strategy_patch.py と
#  旧 core/startup/summary_ai_nve_projection_strategy_patch.py を本文化)
# ------------------------------------------------------------
# 旧構成は usercustomize.py の 3 連 install:
#   strategy_mode -> fibonacci_ev -> nve_projection
# の順で、どちらも本モジュールの _classify / _strategy_enabled を
# closure で捕まえて合成していた。よって実行時は
#   native <- fibonacci_ev <- nve_projection
# の 2 層。同じ順序で重ねている。
#
# 有効/無効は旧 install() と同じ env をそれぞれの層が見る:
#   SUMMARY_AI_FIB_EV_STRATEGY_ENABLED (既定 True)
#   SUMMARY_AI_NVE_STRATEGY_ENABLED    (既定 True)
# ============================================================

_FIB_INSTALLED = False


_FIB_ORIG_CLASSIFY = None


_FIB_ORIG_STRATEGY_ENABLED = None


def _fib_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _fib_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _fib_f(v: Any, default: float = 0.0) -> float:
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


def _fib_side(row: dict[str, Any]) -> str:
    try:
        s = str(row.get("side") or row.get("ai_side") or row.get("entry_decision") or "BUY").strip().upper()
        return "SELL" if s in {"SELL", "SHORT", "1", "売", "売り"} else "BUY"
    except Exception:
        return "BUY"


def _fib_first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    for root_key in ("source_row", "ai_row", "_raw", "raw", "features", "metrics"):
        raw = row.get(root_key)
        if isinstance(raw, dict):
            for k in keys:
                v = raw.get(k)
                if v is not None and str(v).strip() != "":
                    return v
    return default


def _fib_num(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return _fib_f(_fib_first(row, tuple(keys), default), default)


def _fib_pct_distance(price: float, level: float) -> float:
    if price <= 0 or level <= 0:
        return 999.0
    return abs(price - level) / price


def _fib_levels(row: dict[str, Any]) -> tuple[float, float, float]:
    close = _fib_num(row, "close_price", "price", "current_price", "close", "last_price")
    high = _fib_num(
        row,
        "swing_high", "recent_high", "intraday_high", "day_high", "today_high", "range_high",
        "high_1m_max", "high", "high_price", "HighPrice",
    )
    low = _fib_num(
        row,
        "swing_low", "recent_low", "intraday_low", "day_low", "today_low", "range_low",
        "low_1m_min", "low", "low_price", "LowPrice",
    )
    return close, high, low


def _fib_ev_from_row(row: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    # Direct EV / expectancy columns first.
    for key in (
        "expected_value", "expectancy", "ev", "EV", "e_value", "e_calc", "e_calculated",
        "emp_expected_value", "summary_ai_expected_value", "prob_expected_value",
    ):
        raw = _fib_first(row, (key,), None)
        if raw is not None and str(raw).strip() != "":
            return _fib_f(raw, 0.0), {"source": key}

    # Otherwise compute a small EV from empirical target/risk probabilities if present.
    p_target = _fib_num(row, "target_prob", "take_profit_prob", "tp_prob", "emp_target_prob", "prob_take", "p_take", default=-1.0)
    p_risk = _fib_num(row, "risk_prob", "stop_loss_prob", "sl_prob", "emp_risk_prob", "prob_stop", "p_stop", default=-1.0)
    take = _fib_num(row, "take_profit_pct", "target_pct", "tp_pct", default=_fib_env_float("SUMMARY_AI_EV_DEFAULT_TAKE_PCT", 0.002))
    stop = _fib_num(row, "stop_loss_pct", "risk_pct", "sl_pct", default=_fib_env_float("SUMMARY_AI_EV_DEFAULT_STOP_PCT", 0.003))
    # Accept 0-100 or 0-1 probability forms.
    if p_target > 1.0:
        p_target /= 100.0
    if p_risk > 1.0:
        p_risk /= 100.0
    if p_target >= 0.0 and p_risk >= 0.0:
        ev = p_target * abs(take) - p_risk * abs(stop)
        return ev, {"source": "computed_prob", "p_target": p_target, "p_risk": p_risk, "take": take, "stop": stop}
    return None, {"source": "missing"}


def _fib_overlay(row: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(tags, dict):
        return tags
    side = _fib_side(row)
    close, high, low = _fib_levels(row)
    rng = high - low if high > low else 0.0
    reasons = [x for x in str(tags.get("strategy_reasons") or "").split("/") if x]
    fib_score = 0.0
    fib_zone = "NONE"
    fib_retrace = 0.0
    fib_extension = 0.0
    near = _fib_env_float("SUMMARY_AI_FIB_NEAR_PCT", 0.0035)

    if close > 0 and rng > 0:
        if side == "BUY":
            fib382 = high - rng * 0.382
            fib500 = high - rng * 0.500
            fib618 = high - rng * 0.618
            ext1272 = high + rng * 0.272
            ext1618 = high + rng * 0.618
            fib_retrace = (high - close) / rng
            fib_extension = (close - high) / rng if close > high else 0.0
            if fib382 >= close >= fib618:
                fib_score += 1.0
                fib_zone = "BUY_PULLBACK_382_618"
                reasons.append("fib_buy_pullback_382_618")
            if _fib_pct_distance(close, fib500) <= near:
                fib_score += 0.5
                fib_zone = "BUY_PULLBACK_500"
                reasons.append("fib_buy_near_50")
            if close > high:
                fib_score += 0.4
                fib_zone = "BUY_EXTENSION_BREAKOUT"
                reasons.append("fib_buy_extension_breakout")
            if close >= ext1272:
                fib_score -= 0.3
                reasons.append("fib_buy_extension_1272_caution")
            if close >= ext1618:
                fib_score -= 0.8
                reasons.append("fib_buy_extension_1618_overheat")
                # Overextension is more useful as REVERSAL/exit caution than a fresh BUY add.
                tags["strategy_reversal_score"] = round(_fib_f(tags.get("strategy_reversal_score"), 0.0) + 0.8, 4)
        else:
            fib382 = low + rng * 0.382
            fib500 = low + rng * 0.500
            fib618 = low + rng * 0.618
            ext1272 = low - rng * 0.272
            ext1618 = low - rng * 0.618
            fib_retrace = (close - low) / rng
            fib_extension = (low - close) / rng if close < low else 0.0
            if fib382 <= close <= fib618:
                fib_score += 1.0
                fib_zone = "SELL_PULLBACK_382_618"
                reasons.append("fib_sell_pullback_382_618")
            if _fib_pct_distance(close, fib500) <= near:
                fib_score += 0.5
                fib_zone = "SELL_PULLBACK_500"
                reasons.append("fib_sell_near_50")
            if close < low:
                fib_score += 0.4
                fib_zone = "SELL_EXTENSION_BREAKOUT"
                reasons.append("fib_sell_extension_breakout")
            if close <= ext1272:
                fib_score -= 0.3
                reasons.append("fib_sell_extension_1272_caution")
            if close <= ext1618:
                fib_score -= 0.8
                reasons.append("fib_sell_extension_1618_overheat")
                tags["strategy_reversal_score"] = round(_fib_f(tags.get("strategy_reversal_score"), 0.0) + 0.8, 4)

        mode = str(tags.get("strategy_mode") or "").upper()
        if "PULLBACK" in fib_zone:
            tags["strategy_pullback_score"] = round(_fib_f(tags.get("strategy_pullback_score"), 0.0) + max(0.0, fib_score), 4)
            # Prefer PULLBACK when it becomes clearly stronger than breakout.
            if _fib_f(tags.get("strategy_pullback_score"), 0.0) >= _fib_env_float("SUMMARY_AI_STRATEGY_PULLBACK_MIN", 2.0):
                if mode not in {"REVERSAL", "WATCH_ONLY_REVERSAL"}:
                    tags["strategy_mode"] = "PULLBACK"
        elif "EXTENSION_BREAKOUT" in fib_zone:
            tags["strategy_breakout_score"] = round(_fib_f(tags.get("strategy_breakout_score"), 0.0) + max(0.0, fib_score), 4)
        elif fib_score < 0:
            # Do not fully block here; let the strategy guard / blowoff / exit checks do final safety.
            tags["strategy_breakout_score"] = round(_fib_f(tags.get("strategy_breakout_score"), 0.0) + fib_score, 4)

    ev, ev_detail = _fib_ev_from_row(row)
    ev_score = 0.0
    if ev is not None:
        min_positive = _fib_env_float("SUMMARY_AI_EV_POSITIVE_MIN", 0.0)
        if ev > min_positive:
            ev_score = min(0.8, ev / max(0.0001, _fib_env_float("SUMMARY_AI_EV_SCORE_UNIT", 0.002)))
            reasons.append("ev_positive")
        elif ev < _fib_env_float("SUMMARY_AI_EV_NEGATIVE_MAX", -0.0005):
            ev_score = -0.8
            reasons.append("ev_negative")
        # EV is strategy-agnostic quality. Apply to the current winning trend-follow mode.
        mode = str(tags.get("strategy_mode") or "").upper()
        if mode == "PULLBACK":
            tags["strategy_pullback_score"] = round(_fib_f(tags.get("strategy_pullback_score"), 0.0) + ev_score, 4)
        elif mode in {"BREAKOUT", ""}:
            tags["strategy_breakout_score"] = round(_fib_f(tags.get("strategy_breakout_score"), 0.0) + ev_score, 4)
        elif mode == "REVERSAL":
            tags["strategy_reversal_score"] = round(_fib_f(tags.get("strategy_reversal_score"), 0.0) + ev_score, 4)

    # Recompute mode/score after overlay, but keep disabled reversal as watch-only unless explicitly enabled.
    scores = {
        "BREAKOUT": _fib_f(tags.get("strategy_breakout_score"), 0.0),
        "PULLBACK": _fib_f(tags.get("strategy_pullback_score"), 0.0),
        "REVERSAL": _fib_f(tags.get("strategy_reversal_score"), 0.0),
    }
    mode = max(scores, key=scores.get)
    if mode == "REVERSAL" and not _fib_env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
        if scores[mode] < _fib_env_float("SUMMARY_AI_STRATEGY_REVERSAL_STRONG_MIN", 3.2):
            mode = "WATCH_ONLY_REVERSAL"
    tags.update({
        "strategy_mode": mode,
        "strategy_score": round(scores.get(mode.replace("WATCH_ONLY_", ""), scores.get(mode, 0.0)), 4),
        "strategy_fib_score": round(fib_score, 4),
        "strategy_fib_zone": fib_zone,
        "strategy_fib_retrace": round(fib_retrace, 4),
        "strategy_fib_extension": round(fib_extension, 4),
        "strategy_expected_value": None if ev is None else round(float(ev), 6),
        "strategy_ev_score": round(ev_score, 4),
        "strategy_ev_detail": ev_detail,
        "strategy_reasons": "/".join(dict.fromkeys(reasons))[:700],
    })
    return tags


_NVE_INSTALLED = False


_NVE_ORIG_CLASSIFY = None


_NVE_ORIG_STRATEGY_ENABLED = None


def _nve_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _nve_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _nve_f(v: Any, default: float = 0.0) -> float:
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


def _nve_side(row: dict[str, Any]) -> str:
    try:
        s = str(row.get("side") or row.get("ai_side") or row.get("entry_decision") or "BUY").strip().upper()
        return "SELL" if s in {"SELL", "SHORT", "1", "売", "売り"} else "BUY"
    except Exception:
        return "BUY"


def _nve_first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    for root_key in ("source_row", "ai_row", "_raw", "raw", "features", "metrics"):
        raw = row.get(root_key)
        if isinstance(raw, dict):
            for k in keys:
                v = raw.get(k)
                if v is not None and str(v).strip() != "":
                    return v
    return default


def _nve_num(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return _nve_f(_nve_first(row, tuple(keys), default), default)


def _nve_levels(row: dict[str, Any]) -> tuple[float, float, float]:
    close = _nve_num(row, "close_price", "price", "current_price", "close", "last_price")
    high = _nve_num(
        row,
        "swing_high", "recent_high", "intraday_high", "day_high", "today_high", "range_high",
        "high_1m_max", "high", "high_price", "HighPrice",
    )
    low = _nve_num(
        row,
        "swing_low", "recent_low", "intraday_low", "day_low", "today_low", "range_low",
        "low_1m_min", "low", "low_price", "LowPrice",
    )
    return close, high, low


def _nve_near_pct(price: float, target: float) -> float:
    if price <= 0 or target <= 0:
        return 999.0
    return abs(price - target) / price


def _nve_projection(close: float, high: float, low: float, side: str) -> dict[str, float]:
    rng = high - low
    if close <= 0 or high <= 0 or low <= 0 or rng <= 0:
        return {}
    if side == "BUY":
        a = low
        b = high
        c = close
        return {
            "n": c + (b - a),
            "v": b + max(0.0, b - c),
            "e": b + (b - a),
            "range": rng,
            "progress_to_n": (close - c) / max(1e-9, (c + (b - a)) - c),
        }
    a = high
    b = low
    c = close
    return {
        "n": c - (a - b),
        "v": b - max(0.0, c - b),
        "e": b - (a - b),
        "range": rng,
        "progress_to_n": (c - close) / max(1e-9, c - (c - (a - b))),
    }


def _nve_target_room_pct(close: float, target: float, side: str) -> float:
    if close <= 0 or target <= 0:
        return 0.0
    if side == "BUY":
        return (target - close) / close
    return (close - target) / close


def _nve_overlay(row: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(tags, dict):
        return tags
    close, high, low = _nve_levels(row)
    side = _nve_side(row)
    proj = _nve_projection(close, high, low, side)
    reasons = [x for x in str(tags.get("strategy_reasons") or "").split("/") if x]
    nve_score = 0.0
    nve_zone = "NONE"
    nearest_name = ""
    nearest_dist = 999.0
    room_to_n = room_to_v = room_to_e = 0.0

    if proj:
        n = proj["n"]
        v = proj["v"]
        e = proj["e"]
        room_to_n = _nve_target_room_pct(close, n, side)
        room_to_v = _nve_target_room_pct(close, v, side)
        room_to_e = _nve_target_room_pct(close, e, side)
        distances = {"N": _nve_near_pct(close, n), "V": _nve_near_pct(close, v), "E": _nve_near_pct(close, e)}
        nearest_name = min(distances, key=distances.get)
        nearest_dist = float(distances[nearest_name])
        near_pct = _nve_env_float("SUMMARY_AI_NVE_TARGET_NEAR_PCT", 0.0035)
        min_room = _nve_env_float("SUMMARY_AI_NVE_MIN_ROOM_PCT", 0.0020)

        # Positive: target room still exists. This helps avoid buying after the move is already exhausted.
        if room_to_n >= min_room:
            nve_score += 0.6
            reasons.append("n_value_room")
        if room_to_v >= min_room:
            nve_score += 0.4
            reasons.append("v_value_room")
        if room_to_e >= min_room:
            nve_score += 0.3
            reasons.append("e_value_room")

        # Near targets: treat as take-profit / caution zone for fresh entry.
        if nearest_dist <= near_pct:
            nve_zone = f"NEAR_{nearest_name}_VALUE"
            reasons.append(f"near_{nearest_name.lower()}_value_target")
            if nearest_name == "N":
                nve_score -= 0.2
            elif nearest_name == "V":
                nve_score -= 0.4
            elif nearest_name == "E":
                nve_score -= 0.8
                reasons.append("e_value_overextension_caution")
                tags["strategy_reversal_score"] = round(_nve_f(tags.get("strategy_reversal_score"), 0.0) + 0.6, 4)

        # Already beyond targets: do not chase; strong E overshoot is reversal/exit caution.
        if side == "BUY":
            if close >= n:
                nve_score -= 0.25; reasons.append("above_n_value_caution")
            if close >= v:
                nve_score -= 0.45; reasons.append("above_v_value_caution")
            if close >= e:
                nve_score -= 0.9; reasons.append("above_e_value_overheat")
                tags["strategy_reversal_score"] = round(_nve_f(tags.get("strategy_reversal_score"), 0.0) + 0.9, 4)
        else:
            if close <= n:
                nve_score -= 0.25; reasons.append("below_n_value_caution")
            if close <= v:
                nve_score -= 0.45; reasons.append("below_v_value_caution")
            if close <= e:
                nve_score -= 0.9; reasons.append("below_e_value_overheat")
                tags["strategy_reversal_score"] = round(_nve_f(tags.get("strategy_reversal_score"), 0.0) + 0.9, 4)

        mode = str(tags.get("strategy_mode") or "").upper()
        if mode == "PULLBACK":
            tags["strategy_pullback_score"] = round(_nve_f(tags.get("strategy_pullback_score"), 0.0) + nve_score, 4)
        elif mode == "REVERSAL":
            tags["strategy_reversal_score"] = round(_nve_f(tags.get("strategy_reversal_score"), 0.0) + nve_score, 4)
        else:
            tags["strategy_breakout_score"] = round(_nve_f(tags.get("strategy_breakout_score"), 0.0) + nve_score, 4)

        scores = {
            "BREAKOUT": _nve_f(tags.get("strategy_breakout_score"), 0.0),
            "PULLBACK": _nve_f(tags.get("strategy_pullback_score"), 0.0),
            "REVERSAL": _nve_f(tags.get("strategy_reversal_score"), 0.0),
        }
        mode2 = max(scores, key=scores.get)
        if mode2 == "REVERSAL" and not _nve_env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
            if scores[mode2] < _nve_env_float("SUMMARY_AI_STRATEGY_REVERSAL_STRONG_MIN", 3.2):
                mode2 = "WATCH_ONLY_REVERSAL"
        tags["strategy_mode"] = mode2
        tags["strategy_score"] = round(scores.get(mode2.replace("WATCH_ONLY_", ""), scores.get(mode2, 0.0)), 4)
        tags["strategy_n_value"] = round(n, 4)
        tags["strategy_v_value"] = round(v, 4)
        tags["strategy_e_value"] = round(e, 4)

    tags.update({
        "strategy_nve_score": round(nve_score, 4),
        "strategy_nve_zone": nve_zone,
        "strategy_nve_nearest": nearest_name,
        "strategy_nve_nearest_dist_pct": round(nearest_dist, 6) if nearest_dist < 900 else None,
        "strategy_n_room_pct": round(room_to_n, 6),
        "strategy_v_room_pct": round(room_to_v, 6),
        "strategy_e_room_pct": round(room_to_e, 6),
        "strategy_reasons": "/".join(dict.fromkeys(reasons))[:900],
    })
    return tags


def _classify(row: dict[str, Any]) -> dict[str, Any]:
    tags = _classify_core(row)
    if _fib_env_bool("SUMMARY_AI_FIB_EV_STRATEGY_ENABLED", True):
        try:
            tags = _fib_overlay(row, dict(tags or {}))
        except Exception:
            logger.debug("[SUMMARY AI FIB EV] overlay failed row=%s", row, exc_info=True)
    if _nve_env_bool("SUMMARY_AI_NVE_STRATEGY_ENABLED", True):
        try:
            tags = _nve_overlay(row, dict(tags or {}))
        except Exception:
            logger.debug("[SUMMARY AI NVE] overlay failed row=%s", row, exc_info=True)
    return tags


def _strategy_enabled(row: dict[str, Any]) -> tuple[bool, str]:
    ok, reason = _strategy_enabled_core(row)

    # 1層目: 期待値がはっきり負なら弾く (旧 fibonacci_ev)
    if ok and _fib_env_bool("SUMMARY_AI_FIB_EV_STRATEGY_ENABLED", True):
        ev = row.get("strategy_expected_value")
        if ev is not None and _fib_env_bool("SUMMARY_AI_EV_BLOCK_NEGATIVE", True):
            if _fib_f(ev, 0.0) < _fib_env_float("SUMMARY_AI_EV_BLOCK_BELOW", -0.0005):
                return False, "ev_negative"

    # 2層目: 目標が E 値寄り / N 値までの余地が無いなら弾く (旧 nve_projection)
    if ok and _nve_env_bool("SUMMARY_AI_NVE_STRATEGY_ENABLED", True):
        if not _nve_env_bool("SUMMARY_AI_NVE_BLOCK_EXHAUSTED", True):
            return ok, reason
        zone = str(row.get("strategy_nve_zone") or "").upper()
        room_n = _nve_f(row.get("strategy_n_room_pct"), 0.0)
        if zone == "NEAR_E_VALUE":
            return False, "near_e_value_target"
        if room_n < _nve_env_float("SUMMARY_AI_NVE_MIN_ENTRY_ROOM_PCT", 0.0010):
            return False, "n_value_room_too_small"

    return ok, reason
