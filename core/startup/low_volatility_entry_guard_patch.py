# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/low_volatility_entry_guard_patch.py
# Version: V3-ENTRY-ORDER-DAY-RANGE-REPAIR
# ------------------------------------------------------------
# 低ボラ銘柄の最終ガード。
# V3:
#   - entry_order_builder の LOW_MOVE_RANGE_TOO_SMALL が、1tickバーの
#     high=low=close だけを見て SUMMARY_AI を止める問題を補正。
#   - day_high/day_low, high_price/low_price, open-close, range_pct が
#     strict閾値を満たす場合のみ、誤判定として通す。
#   - 閾値は緩和しない。
#
# V2:
#   - range_pct/atr_pct/slope が全部0の場合は、実際の低ボラではなく
#     未計算・欠損の可能性が高いため欠損扱いで fail-open。
#   - 明確な正のrange/atr/change/slopeが複数あり、その全てが閾値未満の時だけ止める。
# ============================================================
from __future__ import annotations

import logging
import math
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-ENTRY-ORDER-DAY-RANGE-REPAIR"
_INSTALLED = False
_ENTRY_ORDER_PATCHED = False


def _env_bool(name: str, default: bool = True) -> bool:
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
        return float(str(v).strip().replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float | None = None) -> float | None:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return float(v)
        s = str(v).strip().replace(",", "").replace("%", "")
        if not s or s.lower() in {"none", "nan", "null", "na", "n/a"}:
            return default
        x = float(s)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        return s if s in {"BUY", "SELL"} else ""
    except Exception:
        return ""


def _merged_dicts(row: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(row, dict):
        out.append(row)
        for k in ("ai_row", "source_row", "row", "summary_row", "candidate", "entry_row", "_raw", "entry_conditions"):
            v = row.get(k)
            if isinstance(v, dict):
                out.append(v)
    return out


def _pick(row: Any, *keys: str) -> Any:
    for d in _merged_dicts(row):
        for k in keys:
            if k in d:
                return d.get(k)
    return None


def _pick_first_positive(row: Any, *keys: str) -> float | None:
    for k in keys:
        v = _safe_float(_pick(row, k), None)
        if v is not None and v > 0:
            return float(v)
    return None


def _abs_pct_value(row: Any, *keys: str) -> float | None:
    vals: list[float] = []
    for k in keys:
        v = _safe_float(_pick(row, k), None)
        if v is None:
            continue
        vals.append(abs(float(v)))
    if not vals:
        return None
    return max(vals)


def _range_pct(row: Any) -> float | None:
    explicit = _abs_pct_value(row, "range_pct", "price_range_pct", "hl_range_pct", "high_low_range_pct", "intraday_range_pct", "display_range_pct")
    if explicit is not None:
        return explicit
    high = _safe_float(_pick(row, "high", "high_price", "ranking_snapshot_high", "day_high", "today_high", "intraday_high", "session_high"), None)
    low = _safe_float(_pick(row, "low", "low_price", "ranking_snapshot_low", "day_low", "today_low", "intraday_low", "session_low"), None)
    close = _safe_float(_pick(row, "close", "close_price", "price", "current_price"), None)
    if high is None or low is None or close is None or close <= 0:
        return None
    if high <= 0 or low <= 0 or high < low:
        return None
    return abs(high - low) / close * 100.0


def _atr_pct(row: Any) -> float | None:
    explicit = _abs_pct_value(row, "atr_pct", "atr_percent", "atr_rate", "atr_ratio_pct", "display_atr_pct")
    if explicit is not None:
        return explicit
    atr = _safe_float(_pick(row, "atr", "ATR", "atr_1m", "atr_3m", "atr_5m"), None)
    close = _safe_float(_pick(row, "close", "close_price", "price", "current_price"), None)
    if atr is None or close is None or close <= 0 or atr < 0:
        return None
    return atr / close * 100.0


def _change_pct(row: Any) -> float | None:
    return _abs_pct_value(row, "change_pct", "change_rate", "change_ratio", "change_percentage", "price_change_pct", "price_change_rate", "price_change_1m", "price_change_3m", "price_change_5m", "pct_change_1m", "pct_change_3m", "pct_change_5m", "return_1m", "return_3m", "return_5m", "momentum_pct")


def _slope_abs(row: Any) -> float | None:
    return _abs_pct_value(row, "slope", "display_slope", "slope_1m", "slope_3m", "slope_5m", "slope_atr_scaled", "display_slope_atr_scaled")


def _best_range_fraction(row: Any) -> tuple[float, float, float, str]:
    close = _pick_first_positive(row, "close", "close_price", "price", "current_price", "last_price") or 0.0
    if close <= 0:
        return 0.0, 0.0, 0.0, "no_close"
    candidates: list[tuple[float, float, float, str]] = []

    def add(label: str, high_keys: tuple[str, ...], low_keys: tuple[str, ...]) -> None:
        high = _pick_first_positive(row, *high_keys) or 0.0
        low = _pick_first_positive(row, *low_keys) or 0.0
        if high > 0 and low > 0 and high >= low:
            candidates.append(((high - low) / close, high, low, label))

    add("bar", ("high",), ("low",))
    add("price_cols", ("high_price",), ("low_price",))
    add("day", ("day_high", "today_high", "intraday_high", "session_high", "high_price_day", "range_high"), ("day_low", "today_low", "intraday_low", "session_low", "low_price_day", "range_low"))

    op = _pick_first_positive(row, "opening_price", "open_price", "open", "day_open") or 0.0
    if op > 0 and close > 0:
        candidates.append((abs(close - op) / close, max(close, op), min(close, op), "open_close"))

    rp = _safe_float(_pick(row, "range_pct", "intraday_range_pct", "day_range_pct"), None)
    if rp is not None and rp > 0:
        if rp > 1.0:
            rp = rp / 100.0
        candidates.append((float(rp), max(close, close * (1.0 + float(rp))), max(0.01, close * (1.0 - float(rp))), "range_pct_col"))

    if not candidates:
        return 0.0, 0.0, 0.0, "missing"
    return max(candidates, key=lambda x: x[0])


def _low_vol_block(row: Any) -> tuple[bool, str, dict[str, Any]]:
    if not _env_bool("ENTRY_LOW_VOLATILITY_GUARD_ENABLED", True):
        return False, "", {}

    min_range_pct = _env_float("ENTRY_MIN_RANGE_PCT", 0.25)
    min_atr_pct = _env_float("ENTRY_MIN_ATR_PCT", 0.12)
    min_change_pct = _env_float("ENTRY_MIN_ABS_CHANGE_PCT", 0.20)
    min_slope_abs = _env_float("ENTRY_MIN_ABS_SLOPE_FOR_VOL", 0.0010)
    min_evidence = int(max(1.0, _env_float("ENTRY_LOW_VOL_MIN_EVIDENCE_COUNT", 2.0)))

    rng = _range_pct(row)
    atr = _atr_pct(row)
    chg = _change_pct(row)
    slp = _slope_abs(row)

    evidence: list[tuple[str, float, float]] = []
    if rng is not None:
        evidence.append(("range_pct", rng, min_range_pct))
    if atr is not None:
        evidence.append(("atr_pct", atr, min_atr_pct))
    if chg is not None:
        evidence.append(("change_pct", chg, min_change_pct))
    if slp is not None:
        evidence.append(("slope_abs", slp, min_slope_abs))

    if _env_bool("ENTRY_LOW_VOL_ZERO_AS_MISSING", True) and evidence:
        positive_evidence = [(name, val, th) for name, val, th in evidence if abs(float(val)) > 1e-12]
        if not positive_evidence:
            return False, "", {"reason": "zero_volatility_fields_treated_as_missing", "range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp, "evidence_count": len(evidence)}
        evidence = positive_evidence

    if len(evidence) < min_evidence:
        return False, "", {"evidence_count": len(evidence), "range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp}

    small = [(name, val, th) for name, val, th in evidence if val < th]
    if len(small) != len(evidence):
        return False, "", {"range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp, "evidence_count": len(evidence)}

    detail = {"symbol": _norm_symbol(_pick(row, "symbol", "Symbol")), "side": _norm_side(_pick(row, "side", "ai_side", "entry_decision")), "range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp, "thresholds": {"min_range_pct": min_range_pct, "min_atr_pct": min_atr_pct, "min_abs_change_pct": min_change_pct, "min_abs_slope": min_slope_abs}, "evidence_count": len(evidence)}
    return True, "low_volatility", detail


def _block_result(row: Any, original: dict[str, Any] | None = None) -> dict[str, Any]:
    blocked, reason, detail = _low_vol_block(row)
    if not blocked:
        return original or {"allow": True, "reason": "ok"}
    logger.warning("[LOW VOL ENTRY GUARD] blocked symbol=%s side=%s detail=%s", detail.get("symbol"), detail.get("side"), detail)
    return {"allow": False, "confidence": 0.0, "lot_multiplier": 0.0, "reason": reason, "model_used": "LOW_VOLATILITY_GUARD", "detail": detail}


def _patch_ai_entry_gate() -> bool:
    try:
        import AI.entry_gate as eg
        cur = getattr(eg, "ai_final_entry_check", None)
        if not callable(cur):
            return False
        if getattr(cur, "_low_volatility_guard_v3", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_ai_final_entry_check(row: dict) -> dict:
            result = orig(row)
            try:
                if isinstance(result, dict) and not bool(result.get("allow")):
                    return result
                blocked, _, _ = _low_vol_block(row)
                if blocked:
                    return _block_result(row, result if isinstance(result, dict) else None)
                return result
            except Exception:
                logger.exception("[LOW VOL ENTRY GUARD] AI.entry_gate wrapper failed; return original result")
                return result

        patched_ai_final_entry_check._low_volatility_guard_v1 = True  # type: ignore[attr-defined]
        patched_ai_final_entry_check._low_volatility_guard_v2 = True  # type: ignore[attr-defined]
        patched_ai_final_entry_check._low_volatility_guard_v3 = True  # type: ignore[attr-defined]
        patched_ai_final_entry_check._original = orig  # type: ignore[attr-defined]
        eg.ai_final_entry_check = patched_ai_final_entry_check
        logger.warning("[LOW VOL ENTRY GUARD] AI.entry_gate patched v3")
        return True
    except Exception:
        logger.debug("[LOW VOL ENTRY GUARD] AI.entry_gate patch skipped", exc_info=True)
        return False


def _patch_summary_ai_selection() -> bool:
    try:
        from trading.entry.summary_ai import executor as e
        cur = getattr(e, "_filter_blocked_ai_ok_items", None)
        if not callable(cur):
            return False
        if getattr(cur, "_low_volatility_guard_v3", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_filter_blocked_ai_ok_items(ok_items):
            if not _env_bool("ENTRY_LOW_VOLATILITY_GUARD_ENABLED", True):
                return orig(ok_items)
            kept = []
            skipped = []
            try:
                for item in list(ok_items or []):
                    if not isinstance(item, dict):
                        kept.append(item)
                        continue
                    blocked, reason, detail = _low_vol_block(item)
                    if blocked:
                        skipped.append({"reason": reason, **detail})
                        continue
                    kept.append(item)
                if skipped:
                    logger.warning("[LOW VOL ENTRY GUARD] SUMMARY_AI prefilter before=%s after=%s skipped=%s", len(ok_items or []), len(kept), skipped[:50])
                return orig(kept)
            except Exception:
                logger.exception("[LOW VOL ENTRY GUARD] SUMMARY_AI prefilter failed; fail-open to original")
                return orig(ok_items)

        patched_filter_blocked_ai_ok_items._low_volatility_guard_v1 = True  # type: ignore[attr-defined]
        patched_filter_blocked_ai_ok_items._low_volatility_guard_v2 = True  # type: ignore[attr-defined]
        patched_filter_blocked_ai_ok_items._low_volatility_guard_v3 = True  # type: ignore[attr-defined]
        patched_filter_blocked_ai_ok_items._original = orig  # type: ignore[attr-defined]
        e._filter_blocked_ai_ok_items = patched_filter_blocked_ai_ok_items
        logger.warning("[LOW VOL ENTRY GUARD] SUMMARY_AI executor prefilter patched v3 zero_as_missing=%s", _env_bool("ENTRY_LOW_VOL_ZERO_AS_MISSING", True))
        return True
    except Exception:
        logger.debug("[LOW VOL ENTRY GUARD] SUMMARY_AI executor patch skipped", exc_info=True)
        return False


def _patch_entry_order_builder_low_move() -> bool:
    global _ENTRY_ORDER_PATCHED
    if _ENTRY_ORDER_PATCHED:
        return True
    try:
        import trading.handlers.entry_order_builder as eob
        cur = getattr(eob, "_low_move_hard_block", None)
        if not callable(cur):
            return False
        if getattr(cur, "_entry_order_day_range_repair_v3", False):
            _ENTRY_ORDER_PATCHED = True
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_low_move_hard_block(entry_row: dict, *, symbol: str, source: str):
            res = orig(entry_row, symbol=symbol, source=source)
            try:
                if not isinstance(res, dict) or res.get("reason") != "LOW_MOVE_RANGE_TOO_SMALL":
                    return res
                if str(source or "").upper() != "SUMMARY_AI":
                    return res
                row = entry_row or {}
                best_range, high, low, method = _best_range_fraction(row)
                min_range = float(getattr(eob, "ENTRY_ORDER_MIN_RANGE_PCT", _env_float("ENTRY_ORDER_MIN_RANGE_PCT", 0.006)))
                if best_range >= min_range:
                    logger.warning(
                        "[LOW VOL ENTRY GUARD] entry_order_builder low-move repaired symbol=%s source=%s range_pct=%.6f min=%.6f high=%.2f low=%.2f method=%s original=%s version=%s",
                        symbol,
                        source,
                        best_range,
                        min_range,
                        high,
                        low,
                        method,
                        res,
                        VERSION,
                    )
                    return None
                return res
            except Exception:
                logger.exception("[LOW VOL ENTRY GUARD] entry_order_builder low-move repair failed; return original")
                return res

        patched_low_move_hard_block._entry_order_day_range_repair_v3 = True  # type: ignore[attr-defined]
        patched_low_move_hard_block._original = orig  # type: ignore[attr-defined]
        eob._low_move_hard_block = patched_low_move_hard_block
        _ENTRY_ORDER_PATCHED = True
        logger.warning("[LOW VOL ENTRY GUARD] entry_order_builder low-move day-range repair patched v3")
        return True
    except Exception:
        logger.debug("[LOW VOL ENTRY GUARD] entry_order_builder patch skipped", exc_info=True)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        # 後段import順で entry_order_builder が後から読み込まれることがあるため、再確認する。
        _patch_entry_order_builder_low_move()
        return True
    if not _env_bool("ENTRY_LOW_VOLATILITY_GUARD_ENABLED", True):
        logger.warning("[LOW VOL ENTRY GUARD] disabled by env")
        return False
    os.environ.setdefault("ENTRY_LOW_VOL_ZERO_AS_MISSING", "1")
    result = {
        "ai_entry_gate": _patch_ai_entry_gate(),
        "summary_ai_selection": _patch_summary_ai_selection(),
        "entry_order_builder": _patch_entry_order_builder_low_move(),
    }
    _INSTALLED = True
    logger.warning("[LOW VOL ENTRY GUARD] installed version=%s result=%s", VERSION, result)
    return any(result.values())


try:
    install()
except Exception:
    logger.exception("[LOW VOL ENTRY GUARD] auto install failed")


__all__ = ["VERSION", "install"]
