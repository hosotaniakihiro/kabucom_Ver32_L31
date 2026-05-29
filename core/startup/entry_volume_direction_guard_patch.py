# ============================================================
# File   : core/startup/entry_volume_direction_guard_patch.py
# Version: V2-HARD-REJECT-COMPAT-FOR-TONOSAMA-GATE
# ------------------------------------------------------------
# 目的:
#   SUMMARY / RANKING / TONOSAMA の3種類すべてのエントリー直前で、
#   出来高急増の方向性・MA5方向・TONOSAMA 3m/5mローソク足を考慮する。
#
# 差し込み位置:
#   trading.handlers.entry_controller.ai_final_entry_check をwrapする。
#
# V2:
#   - V1では allow=False を返していたが、TONOSAMA専用ゲート側が
#     別キー/別理由で OK として扱うケースがあった。
#   - allow / allowed / is_allowed / ai_allow / passed をすべてFalseへ揃える。
#   - confidence / ai_confidence / conf を0へ揃える。
#   - detail/gate_reason/ai_reasonにも拒否理由を入れる。
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


def _merge_reason(new_reason: Any, old_reason: Any) -> str:
    nr = str(new_reason or "").strip()
    old = str(old_reason or "").strip()
    if nr and old and nr not in old:
        return f"{nr};{old}"
    return nr or old


def _force_reject_ai(ai: Any, reason: str, vd: dict) -> dict:
    if isinstance(ai, dict):
        out = dict(ai)
    else:
        out = {}

    for k in ("allow", "allowed", "is_allowed", "ai_allow", "passed", "ok"):
        out[k] = False
    for k in ("confidence", "ai_confidence", "conf"):
        out[k] = 0.0
    out["reason"] = _merge_reason(reason, out.get("reason"))
    out["gate_reason"] = _merge_reason(reason, out.get("gate_reason"))
    out["ai_reason"] = _merge_reason(reason, out.get("ai_reason"))
    out["detail"] = _merge_reason(reason, out.get("detail"))
    out["volume_direction"] = vd
    out["volume_direction_reject"] = True
    out["hard_reject"] = True
    return out


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
            reason = str(vd.get("reason") or "ENTRY_VOLUME_DIRECTION_REJECT")
            logger.warning(
                "[ENTRY VOLUME DIRECTION GUARD] hard reject symbol=%s side=%s reason=%s vol=%.3f move=%.3f trend=%.3f dir=%s source=%s interval=%s",
                symbol,
                vd.get("side"),
                reason,
                float(vd.get("volume_surge_ratio") or 0.0),
                float(vd.get("price_change_pct") or 0.0),
                float(vd.get("trend_score") or 0.0),
                vd.get("trend_direction"),
                entry_row.get("source") if isinstance(entry_row, dict) else None,
                entry_row.get("interval") if isinstance(entry_row, dict) else None,
            )
            return _force_reject_ai(ai, reason, vd)
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
        if getattr(cur, "_entry_volume_direction_guard_v2", False):
            _PATCHED = True
            return True
        _ORIGINAL_AI_GATE = cur
        _patched_ai_final_entry_check._entry_volume_direction_guard_v2 = True  # type: ignore[attr-defined]
        ec.ai_final_entry_check = _patched_ai_final_entry_check
        _PATCHED = True
        logger.warning(
            "[ENTRY VOLUME DIRECTION GUARD] installed V2 hard_reject enabled=%s reject=%s min_surge=%s price_eps=%s",
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
