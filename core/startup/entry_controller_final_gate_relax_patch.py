# ============================================================
# File   : core/startup/entry_controller_final_gate_relax_patch.py
# Version: V1-SUMMARY-TONOSAMA-FINAL-GATE-RELAX
# ------------------------------------------------------------
# 目的:
#   SUMMARY_AI / TONOSAMA は upstream 側ですでに候補承認済みなのに、
#   entry_controller の最終AIゲートだけ BUY score>=5.0 / composite>=5.0 のままで
#   1〜4点台の実運用候補が ORDER_BUILD 前に落ちる問題を補正する。
#
# 方針:
#   - RANKING / EARLY_SCALP は既存ロジックを尊重。
#   - SUMMARY_AI / TONOSAMA だけ source別閾値を緩和。
#   - confidence は維持し、score/composite だけ実運用スキャル用に 1.0 基準へ合わせる。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_PASSES_AI_GATE = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _src(row: dict) -> str:
    try:
        vals = [
            row.get("source"),
            row.get("entry_type"),
            row.get("pipeline_source"),
            row.get("reason"),
            row.get("ai_reason"),
        ]
        return "|".join(str(v or "").upper() for v in vals)
    except Exception:
        return ""


def _is_summary_or_tonosama(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    s = _src(row)
    return "SUMMARY" in s or "SUMMARY_AI" in s or "TONOSAMA" in s


def install() -> bool:
    global _INSTALLED, _ORIG_PASSES_AI_GATE
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_CONTROLLER_FINAL_GATE_RELAX_ENABLED", True):
        logger.warning("[ENTRY FINAL GATE RELAX] disabled by env")
        return False
    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY FINAL GATE RELAX] entry_controller import failed")
        return False

    try:
        # グローバル定数も緩めて、既存ログ/他patchが参照しても一貫するようにする。
        ec.MIN_SUMMARY_SCORE_BUY = _env_float("ENTRY_CONTROLLER_SUMMARY_BUY_MIN_SCORE", 1.0)
        ec.MIN_COMPOSITE_SCORE_BUY = _env_float("ENTRY_CONTROLLER_SUMMARY_BUY_MIN_COMPOSITE", 1.0)
        ec.MIN_SUMMARY_SCORE_SELL = _env_float("ENTRY_CONTROLLER_SUMMARY_SELL_MIN_SCORE", 1.0)
        ec.MIN_COMPOSITE_SCORE_SELL = _env_float("ENTRY_CONTROLLER_SUMMARY_SELL_MIN_COMPOSITE", 1.0)
    except Exception:
        pass

    old = getattr(ec, "_passes_ai_gate", None)
    if not callable(old):
        logger.error("[ENTRY FINAL GATE RELAX] target _passes_ai_gate unavailable")
        return False
    if getattr(old, "_summary_tonosama_final_gate_relax_v1", False):
        _INSTALLED = True
        return True
    _ORIG_PASSES_AI_GATE = old

    def _patched_passes_ai_gate(entry_row: dict, ai: dict, side: str):
        try:
            if not _is_summary_or_tonosama(entry_row):
                return old(entry_row, ai, side)
            if not isinstance(ai, dict):
                return False, "AI_RESULT_INVALID"
            allow = bool(ai.get("allow", False))
            if not allow:
                return False, f"AI_ALLOW_FALSE:{ec._safe_str(ai.get('reason'))}"
            side_n = str(side or "").upper()
            confidence = ec._safe_float(ai.get("confidence"), 0.0)
            score_buy, score_sell = ec._resolve_entry_scores(entry_row, side=side_n)
            src = _src(entry_row)
            if "TONOSAMA" in src:
                min_buy = _env_float("ENTRY_CONTROLLER_TONOSAMA_BUY_MIN_SCORE", 0.01)
                min_sell = _env_float("ENTRY_CONTROLLER_TONOSAMA_SELL_MIN_SCORE", 0.01)
                min_comp_buy = _env_float("ENTRY_CONTROLLER_TONOSAMA_BUY_MIN_COMPOSITE", min_buy)
                min_comp_sell = _env_float("ENTRY_CONTROLLER_TONOSAMA_SELL_MIN_COMPOSITE", min_sell)
            else:
                min_buy = _env_float("ENTRY_CONTROLLER_SUMMARY_BUY_MIN_SCORE", 1.0)
                min_sell = _env_float("ENTRY_CONTROLLER_SUMMARY_SELL_MIN_SCORE", 1.0)
                min_comp_buy = _env_float("ENTRY_CONTROLLER_SUMMARY_BUY_MIN_COMPOSITE", min_buy)
                min_comp_sell = _env_float("ENTRY_CONTROLLER_SUMMARY_SELL_MIN_COMPOSITE", min_sell)

            if side_n == "BUY":
                if confidence < ec.MIN_AI_CONFIDENCE_BUY:
                    return False, f"BUY_CONF_LOW:{confidence:.3f}"
                composite = confidence * score_buy
                if score_buy < min_buy:
                    return False, f"BUY_SCORE_LOW_RELAXED:{score_buy:.3f}<min={min_buy:.3f}"
                if composite < min_comp_buy:
                    return False, f"BUY_COMPOSITE_LOW_RELAXED:{composite:.3f}<min={min_comp_buy:.3f}"
                return True, f"BUY_OK_RELAXED conf={confidence:.3f} score_buy={score_buy:.3f} comp={composite:.3f} src={src}"

            if side_n == "SELL":
                if confidence < ec.MIN_AI_CONFIDENCE_SELL:
                    return False, f"SELL_CONF_LOW:{confidence:.3f}"
                composite = confidence * score_sell
                if score_sell < min_sell:
                    return False, f"SELL_SCORE_LOW_RELAXED:{score_sell:.3f}<min={min_sell:.3f}"
                if composite < min_comp_sell:
                    return False, f"SELL_COMPOSITE_LOW_RELAXED:{composite:.3f}<min={min_comp_sell:.3f}"
                return True, f"SELL_OK_RELAXED conf={confidence:.3f} score_sell={score_sell:.3f} comp={composite:.3f} src={src}"

            return False, f"SIDE_INVALID:{side}"
        except Exception:
            logger.exception("[ENTRY FINAL GATE RELAX] patched gate failed -> fallback original")
            return old(entry_row, ai, side)

    _patched_passes_ai_gate._summary_tonosama_final_gate_relax_v1 = True  # type: ignore[attr-defined]
    _patched_passes_ai_gate._original = old  # type: ignore[attr-defined]
    ec._passes_ai_gate = _patched_passes_ai_gate
    _INSTALLED = True
    logger.warning(
        "[ENTRY FINAL GATE RELAX] installed summary_buy_min=%.3f summary_sell_min=%.3f tonosama_buy_min=%.3f tonosama_sell_min=%.3f",
        _env_float("ENTRY_CONTROLLER_SUMMARY_BUY_MIN_SCORE", 1.0),
        _env_float("ENTRY_CONTROLLER_SUMMARY_SELL_MIN_SCORE", 1.0),
        _env_float("ENTRY_CONTROLLER_TONOSAMA_BUY_MIN_SCORE", 0.01),
        _env_float("ENTRY_CONTROLLER_TONOSAMA_SELL_MIN_SCORE", 0.01),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY FINAL GATE RELAX] auto install failed")

__all__ = ["install"]
