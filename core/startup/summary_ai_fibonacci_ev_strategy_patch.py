# -*- coding: utf-8 -*-
"""
Fibonacci / Expected Value overlay for Summary-AI strategy mode.

This is an additive layer on top of summary_ai_strategy_mode_patch:
- Adds fib retracement zones for PULLBACK entries.
- Adds fib extension / overextension checks for BREAKOUT and REVERSAL risk.
- Adds expected-value fields when empirical probability or EV columns exist.

It does not loosen board, liquidity, ATR, low-volatility, time, or final-entry guards.
If no usable Fibonacci/EV inputs are present, the original strategy classification is
kept unchanged.
"""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-FIBONACCI-EV-OVERLAY-SUMMARY-AI-STRATEGY"
_INSTALLED = False
_ORIG_CLASSIFY = None
_ORIG_STRATEGY_ENABLED = None


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


def _side(row: dict[str, Any]) -> str:
    try:
        s = str(row.get("side") or row.get("ai_side") or row.get("entry_decision") or "BUY").strip().upper()
        return "SELL" if s in {"SELL", "SHORT", "1", "売", "売り"} else "BUY"
    except Exception:
        return "BUY"


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
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


def _num(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return _f(_first(row, tuple(keys), default), default)


def _pct_distance(price: float, level: float) -> float:
    if price <= 0 or level <= 0:
        return 999.0
    return abs(price - level) / price


def _levels(row: dict[str, Any]) -> tuple[float, float, float]:
    close = _num(row, "close_price", "price", "current_price", "close", "last_price")
    high = _num(
        row,
        "swing_high", "recent_high", "intraday_high", "day_high", "today_high", "range_high",
        "high_1m_max", "high", "high_price", "HighPrice",
    )
    low = _num(
        row,
        "swing_low", "recent_low", "intraday_low", "day_low", "today_low", "range_low",
        "low_1m_min", "low", "low_price", "LowPrice",
    )
    return close, high, low


def _ev_from_row(row: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    # Direct EV / expectancy columns first.
    for key in (
        "expected_value", "expectancy", "ev", "EV", "e_value", "e_calc", "e_calculated",
        "emp_expected_value", "summary_ai_expected_value", "prob_expected_value",
    ):
        raw = _first(row, (key,), None)
        if raw is not None and str(raw).strip() != "":
            return _f(raw, 0.0), {"source": key}

    # Otherwise compute a small EV from empirical target/risk probabilities if present.
    p_target = _num(row, "target_prob", "take_profit_prob", "tp_prob", "emp_target_prob", "prob_take", "p_take", default=-1.0)
    p_risk = _num(row, "risk_prob", "stop_loss_prob", "sl_prob", "emp_risk_prob", "prob_stop", "p_stop", default=-1.0)
    take = _num(row, "take_profit_pct", "target_pct", "tp_pct", default=_env_float("SUMMARY_AI_EV_DEFAULT_TAKE_PCT", 0.002))
    stop = _num(row, "stop_loss_pct", "risk_pct", "sl_pct", default=_env_float("SUMMARY_AI_EV_DEFAULT_STOP_PCT", 0.003))
    # Accept 0-100 or 0-1 probability forms.
    if p_target > 1.0:
        p_target /= 100.0
    if p_risk > 1.0:
        p_risk /= 100.0
    if p_target >= 0.0 and p_risk >= 0.0:
        ev = p_target * abs(take) - p_risk * abs(stop)
        return ev, {"source": "computed_prob", "p_target": p_target, "p_risk": p_risk, "take": take, "stop": stop}
    return None, {"source": "missing"}


def _overlay(row: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(tags, dict):
        return tags
    side = _side(row)
    close, high, low = _levels(row)
    rng = high - low if high > low else 0.0
    reasons = [x for x in str(tags.get("strategy_reasons") or "").split("/") if x]
    fib_score = 0.0
    fib_zone = "NONE"
    fib_retrace = 0.0
    fib_extension = 0.0
    near = _env_float("SUMMARY_AI_FIB_NEAR_PCT", 0.0035)

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
            if _pct_distance(close, fib500) <= near:
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
                tags["strategy_reversal_score"] = round(_f(tags.get("strategy_reversal_score"), 0.0) + 0.8, 4)
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
            if _pct_distance(close, fib500) <= near:
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
                tags["strategy_reversal_score"] = round(_f(tags.get("strategy_reversal_score"), 0.0) + 0.8, 4)

        mode = str(tags.get("strategy_mode") or "").upper()
        if "PULLBACK" in fib_zone:
            tags["strategy_pullback_score"] = round(_f(tags.get("strategy_pullback_score"), 0.0) + max(0.0, fib_score), 4)
            # Prefer PULLBACK when it becomes clearly stronger than breakout.
            if _f(tags.get("strategy_pullback_score"), 0.0) >= _env_float("SUMMARY_AI_STRATEGY_PULLBACK_MIN", 2.0):
                if mode not in {"REVERSAL", "WATCH_ONLY_REVERSAL"}:
                    tags["strategy_mode"] = "PULLBACK"
        elif "EXTENSION_BREAKOUT" in fib_zone:
            tags["strategy_breakout_score"] = round(_f(tags.get("strategy_breakout_score"), 0.0) + max(0.0, fib_score), 4)
        elif fib_score < 0:
            # Do not fully block here; let the strategy guard / blowoff / exit checks do final safety.
            tags["strategy_breakout_score"] = round(_f(tags.get("strategy_breakout_score"), 0.0) + fib_score, 4)

    ev, ev_detail = _ev_from_row(row)
    ev_score = 0.0
    if ev is not None:
        min_positive = _env_float("SUMMARY_AI_EV_POSITIVE_MIN", 0.0)
        if ev > min_positive:
            ev_score = min(0.8, ev / max(0.0001, _env_float("SUMMARY_AI_EV_SCORE_UNIT", 0.002)))
            reasons.append("ev_positive")
        elif ev < _env_float("SUMMARY_AI_EV_NEGATIVE_MAX", -0.0005):
            ev_score = -0.8
            reasons.append("ev_negative")
        # EV is strategy-agnostic quality. Apply to the current winning trend-follow mode.
        mode = str(tags.get("strategy_mode") or "").upper()
        if mode == "PULLBACK":
            tags["strategy_pullback_score"] = round(_f(tags.get("strategy_pullback_score"), 0.0) + ev_score, 4)
        elif mode in {"BREAKOUT", ""}:
            tags["strategy_breakout_score"] = round(_f(tags.get("strategy_breakout_score"), 0.0) + ev_score, 4)
        elif mode == "REVERSAL":
            tags["strategy_reversal_score"] = round(_f(tags.get("strategy_reversal_score"), 0.0) + ev_score, 4)

    # Recompute mode/score after overlay, but keep disabled reversal as watch-only unless explicitly enabled.
    scores = {
        "BREAKOUT": _f(tags.get("strategy_breakout_score"), 0.0),
        "PULLBACK": _f(tags.get("strategy_pullback_score"), 0.0),
        "REVERSAL": _f(tags.get("strategy_reversal_score"), 0.0),
    }
    mode = max(scores, key=scores.get)
    if mode == "REVERSAL" and not _env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
        if scores[mode] < _env_float("SUMMARY_AI_STRATEGY_REVERSAL_STRONG_MIN", 3.2):
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


def install() -> bool:
    global _INSTALLED, _ORIG_CLASSIFY, _ORIG_STRATEGY_ENABLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_FIB_EV_STRATEGY_ENABLED", True):
        logger.warning("[SUMMARY AI FIB EV] disabled by env")
        return False
    try:
        from core.startup import summary_ai_strategy_mode_patch as base
        try:
            base.install()
        except Exception:
            logger.debug("[SUMMARY AI FIB EV] base strategy install ignored", exc_info=True)

        cur_classify = getattr(base, "_classify", None)
        if not callable(cur_classify):
            logger.warning("[SUMMARY AI FIB EV] target _classify missing version=%s", VERSION)
            return False
        if not getattr(cur_classify, "_summary_ai_fib_ev_v1", False):
            _ORIG_CLASSIFY = cur_classify

            @wraps(cur_classify)
            def _classify_with_fib_ev(row: dict[str, Any]):
                tags = cur_classify(row)
                try:
                    tags = _overlay(row, dict(tags or {}))
                except Exception:
                    logger.debug("[SUMMARY AI FIB EV] overlay failed row=%s", row, exc_info=True)
                return tags

            _classify_with_fib_ev._summary_ai_fib_ev_v1 = True  # type: ignore[attr-defined]
            _classify_with_fib_ev._original = cur_classify  # type: ignore[attr-defined]
            base._classify = _classify_with_fib_ev

        cur_enabled = getattr(base, "_strategy_enabled", None)
        if callable(cur_enabled) and not getattr(cur_enabled, "_summary_ai_fib_ev_guard_v1", False):
            _ORIG_STRATEGY_ENABLED = cur_enabled

            @wraps(cur_enabled)
            def _strategy_enabled_with_ev(row: dict[str, Any]):
                ok, reason = cur_enabled(row)
                if not ok:
                    return ok, reason
                ev = row.get("strategy_expected_value")
                if ev is not None and _env_bool("SUMMARY_AI_EV_BLOCK_NEGATIVE", True):
                    if _f(ev, 0.0) < _env_float("SUMMARY_AI_EV_BLOCK_BELOW", -0.0005):
                        return False, "ev_negative"
                return ok, reason

            _strategy_enabled_with_ev._summary_ai_fib_ev_guard_v1 = True  # type: ignore[attr-defined]
            _strategy_enabled_with_ev._original = cur_enabled  # type: ignore[attr-defined]
            base._strategy_enabled = _strategy_enabled_with_ev

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI FIB EV] installed version=%s fib_near=%s ev_block_negative=%s reversal_enabled=%s",
            VERSION,
            os.getenv("SUMMARY_AI_FIB_NEAR_PCT", "0.0035"),
            _env_bool("SUMMARY_AI_EV_BLOCK_NEGATIVE", True),
            _env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI FIB EV] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI FIB EV] auto install failed")


__all__ = ["VERSION", "install"]
