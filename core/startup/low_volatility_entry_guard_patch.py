# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/low_volatility_entry_guard_patch.py
# Version: V2-ZERO-VOL-AS-MISSING-SUMMARY-AI
# ------------------------------------------------------------
# 低ボラ銘柄の最終ガード。
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
VERSION = "V2-ZERO-VOL-AS-MISSING-SUMMARY-AI"
_INSTALLED = False


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
        for k in ("ai_row", "source_row", "row", "summary_row", "candidate", "entry_row"):
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
    high = _safe_float(_pick(row, "high", "high_price", "ranking_snapshot_high"), None)
    low = _safe_float(_pick(row, "low", "low_price", "ranking_snapshot_low"), None)
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

    # 0だけのvolatilityは未計算/欠損として扱う。これを低ボラ証拠にすると起動直後のSUMMARY_AIが全落ちする。
    if _env_bool("ENTRY_LOW_VOL_ZERO_AS_MISSING", True) and evidence:
        positive_evidence = [(name, val, th) for name, val, th in evidence if abs(float(val)) > 1e-12]
        if not positive_evidence:
            return False, "", {
                "reason": "zero_volatility_fields_treated_as_missing",
                "range_pct": rng,
                "atr_pct": atr,
                "change_pct": chg,
                "slope_abs": slp,
                "evidence_count": len(evidence),
            }
        evidence = positive_evidence

    if len(evidence) < min_evidence:
        return False, "", {"evidence_count": len(evidence), "range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp}

    small = [(name, val, th) for name, val, th in evidence if val < th]
    if len(small) != len(evidence):
        return False, "", {"range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp, "evidence_count": len(evidence)}

    detail = {
        "symbol": _norm_symbol(_pick(row, "symbol", "Symbol")),
        "side": _norm_side(_pick(row, "side", "ai_side", "entry_decision")),
        "range_pct": rng,
        "atr_pct": atr,
        "change_pct": chg,
        "slope_abs": slp,
        "thresholds": {"min_range_pct": min_range_pct, "min_atr_pct": min_atr_pct, "min_abs_change_pct": min_change_pct, "min_abs_slope": min_slope_abs},
        "evidence_count": len(evidence),
    }
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
        if getattr(cur, "_low_volatility_guard_v2", False):
            return True
        orig = cur

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

        patched_ai_final_entry_check._low_volatility_guard_v2 = True  # type: ignore[attr-defined]
        patched_ai_final_entry_check._low_volatility_guard_v1 = True  # type: ignore[attr-defined]
        patched_ai_final_entry_check._original = orig  # type: ignore[attr-defined]
        eg.ai_final_entry_check = patched_ai_final_entry_check
        logger.warning("[LOW VOL ENTRY GUARD] AI.entry_gate patched v2")
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
        if getattr(cur, "_low_volatility_guard_v2", False):
            return True
        orig = cur

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

        patched_filter_blocked_ai_ok_items._low_volatility_guard_v2 = True  # type: ignore[attr-defined]
        patched_filter_blocked_ai_ok_items._low_volatility_guard_v1 = True  # type: ignore[attr-defined]
        patched_filter_blocked_ai_ok_items._original = orig  # type: ignore[attr-defined]
        e._filter_blocked_ai_ok_items = patched_filter_blocked_ai_ok_items
        logger.warning("[LOW VOL ENTRY GUARD] SUMMARY_AI executor prefilter patched v2 zero_as_missing=%s", _env_bool("ENTRY_LOW_VOL_ZERO_AS_MISSING", True))
        return True
    except Exception:
        logger.debug("[LOW VOL ENTRY GUARD] SUMMARY_AI executor patch skipped", exc_info=True)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_LOW_VOLATILITY_GUARD_ENABLED", True):
        logger.warning("[LOW VOL ENTRY GUARD] disabled by env")
        return False
    os.environ.setdefault("ENTRY_LOW_VOL_ZERO_AS_MISSING", "1")
    result = {"ai_entry_gate": _patch_ai_entry_gate(), "summary_ai_selection": _patch_summary_ai_selection()}
    _INSTALLED = True
    logger.warning("[LOW VOL ENTRY GUARD] installed version=%s result=%s", VERSION, result)
    return any(result.values())


try:
    install()
except Exception:
    logger.exception("[LOW VOL ENTRY GUARD] auto install failed")


__all__ = ["VERSION", "install"]
