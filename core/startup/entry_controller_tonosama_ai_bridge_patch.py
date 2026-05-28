# ============================================================
# File   : core/startup/entry_controller_tonosama_ai_bridge_patch.py
# Version: V1-TONOSAMA-DEDICATED-GATE-AI-BRIDGE
# ------------------------------------------------------------
# 目的:
#   entry_controller では TONOSAMA の場合、先に
#     allow_tonosama_entry() / allow_sell_tonosama_entry()
#   で専用判定を行う。
#
#   その後に SUMMARY 用の汎用 AI.entry_gate も通るため、専用判定OKでも
#   low_turnover などで落ちることがある。
#
# 方針:
#   - source / entry_type が TONOSAMA の場合だけ、専用判定済みとして
#     entry_controller 内のAI結果をTONOSAMA用OKに置き換える。
#   - SUMMARY/RANKING には影響させない。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_AI_CHECK = None
_ORIG_PASS_CHECK = None


def _env_on(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return default
        return float(v)
    except Exception:
        return default


def _su(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_tonosama(row: Any) -> bool:
    try:
        return isinstance(row, dict) and (_su(row.get("source")) == "TONOSAMA" or _su(row.get("entry_type")) == "TONOSAMA")
    except Exception:
        return False


def _score(row: dict, side: str) -> float:
    base = _sf(row.get("score"), 0.0)
    raw = _sf(row.get("_tonosama_score"), 0.0)
    buy = _sf(row.get("score_buy"), base)
    sell = _sf(row.get("score_sell"), 0.0)
    if _su(side) == "SELL":
        return max(abs(base), abs(raw), abs(sell))
    return max(base, raw, buy)


def _ai_check(row: dict):
    try:
        if _env_on("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE", True) and _is_tonosama(row):
            side = _su(row.get("entry_decision") or row.get("side"))
            s = _score(row, side)
            return {
                "allow": True,
                "confidence": 1.0,
                "reason": "TONOSAMA_DEDICATED_GATE_OK",
                "lot_multiplier": 1.0,
                "score": s,
            }
    except Exception:
        logger.debug("[TONOSAMA AI BRIDGE] ai check bridge failed", exc_info=True)
    return _ORIG_AI_CHECK(row)


def _pass_check(row: dict, ai: dict, side: str):
    try:
        if _env_on("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE", True) and _is_tonosama(row):
            s = _score(row, side)
            min_s = _sf(os.getenv("ENTRY_CONTROLLER_TONOSAMA_MIN_SCORE"), 0.01)
            if s < min_s:
                return False, f"TONOSAMA_SCORE_LOW:{s:.3f}<{min_s:.3f}"
            logger.warning(
                "[TONOSAMA AI BRIDGE] accept source=%s entry_type=%s side=%s score=%.4f ai_reason=%s",
                row.get("source"),
                row.get("entry_type"),
                side,
                s,
                ai.get("reason") if isinstance(ai, dict) else "",
            )
            return True, f"TONOSAMA_OK score={s:.3f}"
    except Exception:
        logger.exception("[TONOSAMA AI BRIDGE] pass bridge failed")
    return _ORIG_PASS_CHECK(row, ai, side)


def install() -> bool:
    global _INSTALLED, _ORIG_AI_CHECK, _ORIG_PASS_CHECK
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        cur_ai = getattr(ec, "ai_final_entry_check", None)
        cur_pass = getattr(ec, "_passes_ai_gate", None)
        if not callable(cur_ai) or not callable(cur_pass):
            logger.warning("[TONOSAMA AI BRIDGE] target missing ai=%s pass=%s", callable(cur_ai), callable(cur_pass))
            return False
        if getattr(cur_pass, "_tonosama_ai_bridge_patch", False):
            _INSTALLED = True
            return True
        _ORIG_AI_CHECK = cur_ai
        _ORIG_PASS_CHECK = cur_pass
        _ai_check._tonosama_ai_bridge_patch = True  # type: ignore[attr-defined]
        _pass_check._tonosama_ai_bridge_patch = True  # type: ignore[attr-defined]
        ec.ai_final_entry_check = _ai_check
        ec._passes_ai_gate = _pass_check
        _INSTALLED = True
        logger.warning("[TONOSAMA AI BRIDGE] installed v1 enabled=%s", _env_on("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE", True))
        return True
    except Exception:
        logger.exception("[TONOSAMA AI BRIDGE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA AI BRIDGE] auto install failed")


__all__ = ["install"]
