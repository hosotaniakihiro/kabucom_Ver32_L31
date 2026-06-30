# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/ranking_entry_runtime_rescue_patch.py
# Version: V2-RANKING-ENTRY-CONTROLLER-GATE-AND-ATR-RESCUE
# ------------------------------------------------------------
# Purpose:
#   Ranking entries were reaching entry_controller, but were stopped before
#   order dispatch when startup/rotation data was incomplete:
#     - AI.entry_gate returned mtf_low for RANKING rows even after score,
#       turnover, dominant side, credit, and direct ranking score had passed.
#     - entry_controller imports ai_final_entry_check by value, so patching only
#       AI.entry_gate.ai_final_entry_check did not affect the controller's local
#       reference.
#     - ranking rows could also stop at ATR_1M_FILTER_NG when 1m bars were still
#       too short immediately after startup.
#
#   This patch is intentionally narrow:
#     - Only RANKING rows are rescued from mtf_low.
#     - Only RANKING rows with short/missing 1m ATR are rescued when rank/turnover
#       are strong enough.
#     - Other blocks such as score_low, low_turnover, dominant_low,
#       ranking_ai_mismatch, ranking_direct_low remain fail-closed.
#     - Existing liquidity / position / order guards remain untouched.
# ============================================================
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V2-RANKING-ENTRY-CONTROLLER-GATE-AND-ATR-RESCUE"
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
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(str(v).strip().replace(",", "").replace("%", ""))
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip().replace(",", "")))
    except Exception:
        return int(default)


def _norm_text(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_ranking_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    source = _norm_text(row.get("source") or row.get("entry_source") or row.get("candidate_source") or row.get("pipeline_source"))
    entry_type = _norm_text(row.get("entry_type") or row.get("entryType"))
    if "RANKING" in source or "RANKING" in entry_type:
        return True
    ranking_keys = {
        "rank",
        "ranking_type",
        "ranking_kind",
        "rank_type",
        "source_rank",
        "change_percentage",
        "change_rate",
        "change_ratio",
        "ranking_snapshot_high",
        "ranking_snapshot_low",
    }
    return bool(set(row.keys()) & ranking_keys)


def _ranking_row_strong_enough_for_atr_rescue(row: Any) -> bool:
    if not isinstance(row, dict) or not _is_ranking_row(row):
        return False
    rank = _safe_int(row.get("rank") or row.get("rank_position") or row.get("source_rank"), 9999)
    score = _safe_float(row.get("score") or row.get("score_buy") or row.get("final_score") or row.get("score_total"), 0.0)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    max_rank = _safe_int(os.getenv("RANKING_ATR_SHORT_RESCUE_MAX_RANK"), 30)
    min_score = _env_float("RANKING_ATR_SHORT_RESCUE_MIN_SCORE", 55.0)
    min_turnover = _env_float("RANKING_ATR_SHORT_RESCUE_MIN_TURNOVER", 100_000_000.0)
    min_volume = _env_float("RANKING_ATR_SHORT_RESCUE_MIN_VOLUME", 30_000.0)
    return rank <= max_rank and score >= min_score and turnover >= min_turnover and volume >= min_volume


def _rescue_mtf_low_result(row: dict, result: dict) -> dict:
    if not _env_bool("RANKING_ENTRY_MTF_LOW_FAIL_OPEN", True):
        return result
    if not isinstance(result, dict) or bool(result.get("allow")):
        return result
    if not _is_ranking_row(row):
        return result
    reason = str(result.get("reason") or "")
    if reason != "mtf_low" and not reason.startswith("mtf_low"):
        return result

    conf = max(
        _safe_float(result.get("confidence"), 0.0),
        _env_float("RANKING_ENTRY_MTF_LOW_RESCUE_CONFIDENCE", 0.72),
    )
    lot_multiplier = max(0.5, min(2.0, 0.5 + conf))
    rescued = {
        "allow": True,
        "confidence": conf,
        "lot_multiplier": lot_multiplier,
        "reason": f"{reason}|ranking_mtf_low_failopen",
        "model_used": str(result.get("model_used") or "MTF") + "+RANKING_MTF_RESCUE",
    }
    logger.warning(
        "[RANKING ENTRY RUNTIME RESCUE] mtf_low rescued symbol=%s side=%s conf=%.3f score=%s source=%s",
        row.get("symbol"),
        row.get("side") or row.get("entry_decision"),
        conf,
        row.get("score") or row.get("final_score") or row.get("score_total"),
        row.get("source"),
    )
    return rescued


def _build_gate_wrapper(orig):
    @wraps(orig)
    def patched_ai_final_entry_check(row: dict) -> dict:
        result = orig(row)
        try:
            return _rescue_mtf_low_result(row, result)
        except Exception:
            logger.exception("[RANKING ENTRY RUNTIME RESCUE] entry gate wrapper failed; return original result")
            return result

    patched_ai_final_entry_check._ranking_runtime_rescue_v2 = True  # type: ignore[attr-defined]
    patched_ai_final_entry_check._ranking_runtime_rescue_v1 = True  # type: ignore[attr-defined]
    patched_ai_final_entry_check._original = getattr(orig, "_original", orig)  # type: ignore[attr-defined]
    return patched_ai_final_entry_check


def _patch_entry_gate() -> bool:
    ok = False
    try:
        import AI.entry_gate as eg

        cur = getattr(eg, "ai_final_entry_check", None)
        if callable(cur) and not getattr(cur, "_ranking_runtime_rescue_v2", False):
            eg.ai_final_entry_check = _build_gate_wrapper(getattr(cur, "_original", cur))
            ok = True
            logger.warning("[RANKING ENTRY RUNTIME RESCUE] AI.entry_gate patched for ranking mtf_low fail-open v2")
    except Exception:
        logger.debug("[RANKING ENTRY RUNTIME RESCUE] entry_gate patch skipped", exc_info=True)

    # entry_controller imported ai_final_entry_check by value, so patch its local
    # reference too. This is the key fix for 2026-06-30 14:33 logs.
    try:
        import trading.handlers.entry_controller as ec

        cur2 = getattr(ec, "ai_final_entry_check", None)
        if callable(cur2) and not getattr(cur2, "_ranking_runtime_rescue_v2", False):
            ec.ai_final_entry_check = _build_gate_wrapper(getattr(cur2, "_original", cur2))
            ok = True
            logger.warning("[RANKING ENTRY RUNTIME RESCUE] entry_controller.ai_final_entry_check patched for ranking mtf_low fail-open v2")
    except Exception:
        logger.debug("[RANKING ENTRY RUNTIME RESCUE] entry_controller gate patch skipped", exc_info=True)

    return ok


def _patch_high_low_snapshot_zero_range() -> bool:
    try:
        import core.startup.ranking_entry_high_low_from_snapshot_patch as hl

        cur = getattr(hl, "_needs_high_low", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_zero_range_rescue_v2", False):
            return True

        def patched_needs_high_low(entry_row: dict[str, Any]) -> bool:
            high = _safe_float(entry_row.get("high") or entry_row.get("high_price"), 0.0)
            low = _safe_float(entry_row.get("low") or entry_row.get("low_price"), 0.0)
            close = _safe_float(entry_row.get("close") or entry_row.get("close_price") or entry_row.get("price"), 0.0)
            # Existing code only refilled when high/low were missing or inverted.
            # For ranking startup rows, high == low == close is also unusable
            # because volatility/range filters see a zero range.
            return close > 0 and (high <= 0 or low <= 0 or high <= low)

        patched_needs_high_low._ranking_zero_range_rescue_v2 = True  # type: ignore[attr-defined]
        patched_needs_high_low._ranking_zero_range_rescue_v1 = True  # type: ignore[attr-defined]
        patched_needs_high_low._original = cur  # type: ignore[attr-defined]
        hl._needs_high_low = patched_needs_high_low
        logger.warning("[RANKING ENTRY RUNTIME RESCUE] high/low snapshot patch now refills zero-range rows v2")
        return True
    except Exception:
        logger.debug("[RANKING ENTRY RUNTIME RESCUE] high/low zero-range patch skipped", exc_info=True)
        return False


def _patch_volatility_atr_short_rescue() -> bool:
    try:
        import trading.filters.volatility_filter as vf

        cur = getattr(vf, "atr_1m_filter", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_atr_short_rescue_v2", False):
            return True

        @wraps(cur)
        def patched_atr_1m_filter(*args, **kwargs):
            out = cur(*args, **kwargs)
            try:
                # entry_controller calls atr_1m_filter(entry_row) and expects bool.
                if isinstance(out, bool) and out is False and args:
                    row = args[0]
                    if _env_bool("RANKING_ATR_SHORT_FAIL_OPEN", True) and _ranking_row_strong_enough_for_atr_rescue(row):
                        logger.warning(
                            "[RANKING ENTRY RUNTIME RESCUE] ATR short rescued symbol=%s rank=%s score=%s turnover=%s volume=%s",
                            row.get("symbol") if isinstance(row, dict) else None,
                            row.get("rank") if isinstance(row, dict) else None,
                            row.get("score") if isinstance(row, dict) else None,
                            row.get("turnover") if isinstance(row, dict) else None,
                            row.get("volume") if isinstance(row, dict) else None,
                        )
                        return True
            except Exception:
                logger.exception("[RANKING ENTRY RUNTIME RESCUE] ATR short rescue wrapper failed")
            return out

        patched_atr_1m_filter._ranking_atr_short_rescue_v2 = True  # type: ignore[attr-defined]
        patched_atr_1m_filter._original = getattr(cur, "_original", cur)  # type: ignore[attr-defined]
        vf.atr_1m_filter = patched_atr_1m_filter

        try:
            import trading.handlers.entry_controller as ec
            ec.atr_1m_filter = patched_atr_1m_filter
        except Exception:
            pass

        logger.warning("[RANKING ENTRY RUNTIME RESCUE] volatility atr_1m_filter patched for strong ranking short-history rescue v2")
        return True
    except Exception:
        logger.debug("[RANKING ENTRY RUNTIME RESCUE] volatility ATR rescue patch skipped", exc_info=True)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("RANKING_ENTRY_RUNTIME_RESCUE_ENABLED", True):
        logger.warning("[RANKING ENTRY RUNTIME RESCUE] disabled by env")
        return False
    result = {
        "entry_gate": _patch_entry_gate(),
        "high_low_zero_range": _patch_high_low_snapshot_zero_range(),
        "atr_short_rescue": _patch_volatility_atr_short_rescue(),
    }
    _INSTALLED = True
    logger.warning("[RANKING ENTRY RUNTIME RESCUE] installed version=%s result=%s", VERSION, result)
    return any(result.values())


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY RUNTIME RESCUE] auto install failed")


__all__ = ["VERSION", "install"]
