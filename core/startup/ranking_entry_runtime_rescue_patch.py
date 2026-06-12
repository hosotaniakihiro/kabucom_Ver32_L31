# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/ranking_entry_runtime_rescue_patch.py
# Version: V1-RANKING-ENTRY-MTF-LOW-AND-ZERO-RANGE-RESCUE
# ------------------------------------------------------------
# Purpose:
#   Ranking entries were reaching entry_controller, but were stopped before
#   order dispatch when startup/rotation data was incomplete:
#     - AI.entry_gate returned mtf_low for RANKING rows even after score,
#       turnover, dominant side, credit, and direct ranking score had passed.
#     - ranking entry rows with high == low == close kept a zero range and
#       could still be filtered despite ranking_snapshot movement rescue.
#
#   This patch is intentionally narrow:
#     - Only RANKING rows are rescued from mtf_low.
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
VERSION = "V1-RANKING-ENTRY-MTF-LOW-AND-ZERO-RANGE-RESCUE"
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
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(str(v).strip().replace(",", "").replace("%", ""))
    except Exception:
        return default


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


def _patch_entry_gate() -> bool:
    try:
        import AI.entry_gate as eg

        cur = getattr(eg, "ai_final_entry_check", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_runtime_rescue_v1", False):
            return True
        orig = cur

        @wraps(orig)
        def patched_ai_final_entry_check(row: dict) -> dict:
            result = orig(row)
            try:
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
            except Exception:
                logger.exception("[RANKING ENTRY RUNTIME RESCUE] entry gate wrapper failed; return original result")
                return result

        patched_ai_final_entry_check._ranking_runtime_rescue_v1 = True  # type: ignore[attr-defined]
        patched_ai_final_entry_check._original = orig  # type: ignore[attr-defined]
        eg.ai_final_entry_check = patched_ai_final_entry_check
        logger.warning("[RANKING ENTRY RUNTIME RESCUE] AI.entry_gate patched for ranking mtf_low fail-open")
        return True
    except Exception:
        logger.debug("[RANKING ENTRY RUNTIME RESCUE] entry_gate patch skipped", exc_info=True)
        return False


def _patch_high_low_snapshot_zero_range() -> bool:
    try:
        import core.startup.ranking_entry_high_low_from_snapshot_patch as hl

        cur = getattr(hl, "_needs_high_low", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_zero_range_rescue_v1", False):
            return True

        def patched_needs_high_low(entry_row: dict[str, Any]) -> bool:
            high = _safe_float(entry_row.get("high") or entry_row.get("high_price"), 0.0)
            low = _safe_float(entry_row.get("low") or entry_row.get("low_price"), 0.0)
            close = _safe_float(entry_row.get("close") or entry_row.get("close_price") or entry_row.get("price"), 0.0)
            # Existing code only refilled when high/low were missing or inverted.
            # For ranking startup rows, high == low == close is also unusable
            # because volatility/range filters see a zero range.
            return close > 0 and (high <= 0 or low <= 0 or high <= low)

        patched_needs_high_low._ranking_zero_range_rescue_v1 = True  # type: ignore[attr-defined]
        patched_needs_high_low._original = cur  # type: ignore[attr-defined]
        hl._needs_high_low = patched_needs_high_low
        logger.warning("[RANKING ENTRY RUNTIME RESCUE] high/low snapshot patch now refills zero-range rows")
        return True
    except Exception:
        logger.debug("[RANKING ENTRY RUNTIME RESCUE] high/low zero-range patch skipped", exc_info=True)
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
    }
    _INSTALLED = True
    logger.warning("[RANKING ENTRY RUNTIME RESCUE] installed version=%s result=%s", VERSION, result)
    return any(result.values())


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY RUNTIME RESCUE] auto install failed")


__all__ = ["VERSION", "install"]
