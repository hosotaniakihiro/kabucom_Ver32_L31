# -*- coding: utf-8 -*-
"""
N/V/E price projection overlay for Summary-AI strategy mode.

Ichimoku price-observation style targets:
BUY side with A=low, B=high, C=current/pullback:
    N = C + (B - A)
    V = B + (B - C)
    E = B + (B - A)
SELL side with A=high, B=low, C=current/rebound:
    N = C - (A - B)
    V = B - (C - B)
    E = B - (A - B)

This is an additive strategy overlay only. It does not loosen board, liquidity,
ATR, low-volatility, time, position, or final-entry guards.
"""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-NVE-PRICE-PROJECTION-OVERLAY-SUMMARY-AI-STRATEGY"
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


def _near_pct(price: float, target: float) -> float:
    if price <= 0 or target <= 0:
        return 999.0
    return abs(price - target) / price


def _projection(close: float, high: float, low: float, side: str) -> dict[str, float]:
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


def _target_room_pct(close: float, target: float, side: str) -> float:
    if close <= 0 or target <= 0:
        return 0.0
    if side == "BUY":
        return (target - close) / close
    return (close - target) / close


def _overlay(row: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(tags, dict):
        return tags
    close, high, low = _levels(row)
    side = _side(row)
    proj = _projection(close, high, low, side)
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
        room_to_n = _target_room_pct(close, n, side)
        room_to_v = _target_room_pct(close, v, side)
        room_to_e = _target_room_pct(close, e, side)
        distances = {"N": _near_pct(close, n), "V": _near_pct(close, v), "E": _near_pct(close, e)}
        nearest_name = min(distances, key=distances.get)
        nearest_dist = float(distances[nearest_name])
        near_pct = _env_float("SUMMARY_AI_NVE_TARGET_NEAR_PCT", 0.0035)
        min_room = _env_float("SUMMARY_AI_NVE_MIN_ROOM_PCT", 0.0020)

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
                tags["strategy_reversal_score"] = round(_f(tags.get("strategy_reversal_score"), 0.0) + 0.6, 4)

        # Already beyond targets: do not chase; strong E overshoot is reversal/exit caution.
        if side == "BUY":
            if close >= n:
                nve_score -= 0.25; reasons.append("above_n_value_caution")
            if close >= v:
                nve_score -= 0.45; reasons.append("above_v_value_caution")
            if close >= e:
                nve_score -= 0.9; reasons.append("above_e_value_overheat")
                tags["strategy_reversal_score"] = round(_f(tags.get("strategy_reversal_score"), 0.0) + 0.9, 4)
        else:
            if close <= n:
                nve_score -= 0.25; reasons.append("below_n_value_caution")
            if close <= v:
                nve_score -= 0.45; reasons.append("below_v_value_caution")
            if close <= e:
                nve_score -= 0.9; reasons.append("below_e_value_overheat")
                tags["strategy_reversal_score"] = round(_f(tags.get("strategy_reversal_score"), 0.0) + 0.9, 4)

        mode = str(tags.get("strategy_mode") or "").upper()
        if mode == "PULLBACK":
            tags["strategy_pullback_score"] = round(_f(tags.get("strategy_pullback_score"), 0.0) + nve_score, 4)
        elif mode == "REVERSAL":
            tags["strategy_reversal_score"] = round(_f(tags.get("strategy_reversal_score"), 0.0) + nve_score, 4)
        else:
            tags["strategy_breakout_score"] = round(_f(tags.get("strategy_breakout_score"), 0.0) + nve_score, 4)

        scores = {
            "BREAKOUT": _f(tags.get("strategy_breakout_score"), 0.0),
            "PULLBACK": _f(tags.get("strategy_pullback_score"), 0.0),
            "REVERSAL": _f(tags.get("strategy_reversal_score"), 0.0),
        }
        mode2 = max(scores, key=scores.get)
        if mode2 == "REVERSAL" and not _env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
            if scores[mode2] < _env_float("SUMMARY_AI_STRATEGY_REVERSAL_STRONG_MIN", 3.2):
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


def install() -> bool:
    global _INSTALLED, _ORIG_CLASSIFY, _ORIG_STRATEGY_ENABLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_NVE_STRATEGY_ENABLED", True):
        logger.warning("[SUMMARY AI NVE] disabled by env")
        return False
    try:
        from core.startup import summary_ai_strategy_mode_patch as base
        try:
            base.install()
        except Exception:
            logger.debug("[SUMMARY AI NVE] base strategy install ignored", exc_info=True)

        cur_classify = getattr(base, "_classify", None)
        if not callable(cur_classify):
            logger.warning("[SUMMARY AI NVE] target _classify missing version=%s", VERSION)
            return False
        if not getattr(cur_classify, "_summary_ai_nve_v1", False):
            _ORIG_CLASSIFY = cur_classify

            @wraps(cur_classify)
            def _classify_with_nve(row: dict[str, Any]):
                tags = cur_classify(row)
                try:
                    tags = _overlay(row, dict(tags or {}))
                except Exception:
                    logger.debug("[SUMMARY AI NVE] overlay failed row=%s", row, exc_info=True)
                return tags

            _classify_with_nve._summary_ai_nve_v1 = True  # type: ignore[attr-defined]
            _classify_with_nve._original = cur_classify  # type: ignore[attr-defined]
            base._classify = _classify_with_nve

        cur_enabled = getattr(base, "_strategy_enabled", None)
        if callable(cur_enabled) and not getattr(cur_enabled, "_summary_ai_nve_guard_v1", False):
            _ORIG_STRATEGY_ENABLED = cur_enabled

            @wraps(cur_enabled)
            def _strategy_enabled_with_nve(row: dict[str, Any]):
                ok, reason = cur_enabled(row)
                if not ok:
                    return ok, reason
                if not _env_bool("SUMMARY_AI_NVE_BLOCK_EXHAUSTED", True):
                    return ok, reason
                zone = str(row.get("strategy_nve_zone") or "").upper()
                room_n = _f(row.get("strategy_n_room_pct"), 0.0)
                # Avoid fresh entry when the nearest target is E or when N room is essentially gone.
                if zone == "NEAR_E_VALUE":
                    return False, "near_e_value_target"
                if room_n < _env_float("SUMMARY_AI_NVE_MIN_ENTRY_ROOM_PCT", 0.0010):
                    return False, "n_value_room_too_small"
                return ok, reason

            _strategy_enabled_with_nve._summary_ai_nve_guard_v1 = True  # type: ignore[attr-defined]
            _strategy_enabled_with_nve._original = cur_enabled  # type: ignore[attr-defined]
            base._strategy_enabled = _strategy_enabled_with_nve

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI NVE] installed version=%s near_pct=%s min_room=%s block_exhausted=%s",
            VERSION,
            os.getenv("SUMMARY_AI_NVE_TARGET_NEAR_PCT", "0.0035"),
            os.getenv("SUMMARY_AI_NVE_MIN_ROOM_PCT", "0.0020"),
            _env_bool("SUMMARY_AI_NVE_BLOCK_EXHAUSTED", True),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI NVE] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI NVE] auto install failed")


__all__ = ["VERSION", "install"]
