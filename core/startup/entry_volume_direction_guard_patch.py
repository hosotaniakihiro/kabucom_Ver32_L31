# ============================================================
# File   : core/startup/entry_volume_direction_guard_patch.py
# Version: V1-ALL-SOURCES-VOLUME-DIRECTION-GUARD
# ------------------------------------------------------------
# 目的:
#   SUMMARY / RANKING / TONOSAMA の3種類すべてのエントリー直前で、
#   出来高急増の方向性を考慮する。
#
# 差し込み位置:
#   trading.handlers.entry_controller.ai_final_entry_check をwrapする。
#   ここなら3種類すべての候補が最終AI gateに到達する直前/直後で共通判定できる。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_AI_GATE = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_symbol(row: Any) -> str:
    try:
        if isinstance(row, dict):
            return str(row.get("symbol") or row.get("Symbol") or "").strip()
    except Exception:
        pass
    return ""


def _patched_ai_final_entry_check(entry_row: dict, *args: Any, **kwargs: Any):
    ai = _ORIGINAL_AI_GATE(entry_row, *args, **kwargs)
    if not _env_bool("ENTRY_VOLUME_DIRECTION_GUARD", True):
        return ai
    try:
        from trading.entry.volume_direction_guard import evaluate_volume_direction
        vd = evaluate_volume_direction(entry_row if isinstance(entry_row, dict) else {})
        if isinstance(ai, dict):
            ai = dict(ai)
            ai["volume_direction"] = vd
        if not vd.get("ok", True):
            symbol = _safe_symbol(entry_row)
            logger.warning(
                "[ENTRY VOLUME DIRECTION GUARD] reject symbol=%s side=%s reason=%s vol=%.3f move=%.3f trend=%.3f dir=%s source=%s interval=%s",
                symbol,
                vd.get("side"),
                vd.get("reason"),
                float(vd.get("volume_surge_ratio") or 0.0),
                float(vd.get("price_change_pct") or 0.0),
                float(vd.get("trend_score") or 0.0),
                vd.get("trend_direction"),
                entry_row.get("source") if isinstance(entry_row, dict) else None,
                entry_row.get("interval") if isinstance(entry_row, dict) else None,
            )
            if isinstance(ai, dict):
                ai["allow"] = False
                prev_reason = str(ai.get("reason") or "")
                ai["reason"] = f"{vd.get('reason')};{prev_reason}" if prev_reason else str(vd.get("reason"))
                ai["confidence"] = min(float(ai.get("confidence") or 0.0), 0.0)
                return ai
            return {"allow": False, "confidence": 0.0, "reason": str(vd.get("reason")), "volume_direction": vd}
        else:
            if vd.get("action") == "WARN":
                logger.warning(
                    "[ENTRY VOLUME DIRECTION GUARD] warn-only symbol=%s side=%s reason=%s vol=%.3f move=%.3f trend=%.3f dir=%s",
                    _safe_symbol(entry_row),
                    vd.get("side"),
                    vd.get("reason"),
                    float(vd.get("volume_surge_ratio") or 0.0),
                    float(vd.get("price_change_pct") or 0.0),
                    float(vd.get("trend_score") or 0.0),
                    vd.get("trend_direction"),
                )
        return ai
    except Exception:
        logger.exception("[ENTRY VOLUME DIRECTION GUARD] evaluation failed")
        return ai


def install() -> bool:
    global _PATCHED, _ORIGINAL_AI_GATE
    if _PATCHED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "ai_final_entry_check", None)
        if not callable(cur):
            logger.warning("[ENTRY VOLUME DIRECTION GUARD] ai_final_entry_check unavailable")
            return False
        if getattr(cur, "_entry_volume_direction_guard_v1", False):
            _PATCHED = True
            return True
        _ORIGINAL_AI_GATE = cur
        _patched_ai_final_entry_check._entry_volume_direction_guard_v1 = True  # type: ignore[attr-defined]
        ec.ai_final_entry_check = _patched_ai_final_entry_check
        _PATCHED = True
        logger.warning(
            "[ENTRY VOLUME DIRECTION GUARD] installed enabled=%s reject=%s min_surge=%s price_eps=%s",
            _env_bool("ENTRY_VOLUME_DIRECTION_GUARD", True),
            _env_bool("ENTRY_VOLUME_DIRECTION_REJECT", True),
            os.getenv("ENTRY_VOLUME_DIRECTION_MIN_SURGE_RATIO", "2.0"),
            os.getenv("ENTRY_VOLUME_DIRECTION_PRICE_EPS_PCT", "0.15"),
        )
        return True
    except Exception:
        logger.exception("[ENTRY VOLUME DIRECTION GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY VOLUME DIRECTION GUARD] auto install failed")


__all__ = ["install"]
