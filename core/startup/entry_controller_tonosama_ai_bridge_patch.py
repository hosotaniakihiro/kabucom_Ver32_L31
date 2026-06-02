# ============================================================
# File   : core/startup/entry_controller_tonosama_ai_bridge_patch.py
# Version: V3-TONOSAMA-BRIDGE-DEDICATED-OK-WHITELIST
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
#
# V3:
#   - TONOSAMA_DEDICATED_GATE_OK / TONOSAMA_OK を明示的にOK理由として扱う。
#   - ai dict に古い reject=True 等が残っていても、OK理由なら hard_reject と誤判定しない。
#   - OK dict に gate_reason / ai_reason / detail も入れて後段判定を安定化。
#
# V2:
#   - ENTRY VOLUME DIRECTION GUARD / 3m5m candle guard 等の hard_reject を尊重する。
#   - ai結果に hard_reject / volume_direction_reject / allow=False 等がある場合、
#     TONOSAMA_DEDICATED_GATE_OK で上書きしない。
#   - 拒否理由に TONOSAMA_*_REJECT / *_REJECT_* が含まれる場合もNGを維持する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_AI_CHECK = None
_ORIG_PASS_CHECK = None

_REJECT_TOKENS = (
    "HARD_REJECT",
    "VOLUME_DIRECTION_REJECT",
    "TONOSAMA_BUY_REJECT",
    "TONOSAMA_SELL_REJECT",
    "TONOSAMA_REJECT",
    "REJECT_3M5M",
    "3M5M_CANDLE_REJECT",
    "NO_STRONG_3M5M",
    "MA5_DOWN_AND_PRICE_BELOW",
    "MA5_UP_AND_PRICE_ABOVE",
    "VOLUME_SURGE_AFTER_UP_MOVE",
    "VOLUME_SURGE_AFTER_DOWN_MOVE",
)

_OK_TOKENS = (
    "TONOSAMA_DEDICATED_GATE_OK",
    "TONOSAMA_OK",
    "RULE_OK",
)


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


def _joined_text(*vals: Any) -> str:
    try:
        return " ".join(str(v or "") for v in vals).upper()
    except Exception:
        return ""


def _txt_contains_reject(*vals: Any) -> bool:
    text = _joined_text(*vals)
    return any(tok in text for tok in _REJECT_TOKENS)


def _txt_contains_ok(*vals: Any) -> bool:
    text = _joined_text(*vals)
    return any(tok in text for tok in _OK_TOKENS)


def _ai_is_hard_reject(ai: Any) -> tuple[bool, str]:
    if not isinstance(ai, dict):
        return False, ""
    try:
        reason_text = _joined_text(
            ai.get("reason"),
            ai.get("gate_reason"),
            ai.get("ai_reason"),
            ai.get("detail"),
        )

        # 最重要: TONOSAMA専用ゲートOKは、古い reject フラグが残っていてもNG扱いしない。
        if _txt_contains_ok(reason_text):
            return False, ""

        for k in ("hard_reject", "volume_direction_reject", "reject", "is_reject"):
            if bool(ai.get(k)):
                return True, str(ai.get("reason") or ai.get("gate_reason") or ai.get("ai_reason") or k)

        # 明示的なFalse系を尊重する。ただし理由がOKなら上で除外済み。
        for k in ("allow", "allowed", "is_allowed", "ai_allow", "passed", "ok"):
            if k in ai and ai.get(k) is False:
                reason = str(ai.get("reason") or ai.get("gate_reason") or ai.get("ai_reason") or k)
                if _txt_contains_reject(reason, ai.get("detail"), ai.get("volume_direction")):
                    return True, reason

        if _txt_contains_reject(ai.get("reason"), ai.get("gate_reason"), ai.get("ai_reason"), ai.get("detail"), ai.get("volume_direction")):
            return True, str(ai.get("reason") or ai.get("gate_reason") or ai.get("ai_reason") or "TONOSAMA_HARD_REJECT_REASON")
    except Exception:
        pass
    return False, ""


def _force_ng(reason: str, ai: Any | None = None) -> dict:
    out = dict(ai) if isinstance(ai, dict) else {}
    for k in ("allow", "allowed", "is_allowed", "ai_allow", "passed", "ok"):
        out[k] = False
    for k in ("confidence", "ai_confidence", "conf"):
        out[k] = 0.0
    out["reason"] = reason or out.get("reason") or "TONOSAMA_HARD_REJECT"
    out["gate_reason"] = reason or out.get("gate_reason") or "TONOSAMA_HARD_REJECT"
    out["ai_reason"] = reason or out.get("ai_reason") or "TONOSAMA_HARD_REJECT"
    out["hard_reject"] = True
    out["tonosama_bridge_respected_reject"] = True
    return out


def _force_ok(row: dict, side: str) -> dict:
    s = _score(row, side)
    reason = "TONOSAMA_DEDICATED_GATE_OK"
    return {
        "allow": True,
        "allowed": True,
        "is_allowed": True,
        "ai_allow": True,
        "passed": True,
        "ok": True,
        "reject": False,
        "is_reject": False,
        "hard_reject": False,
        "volume_direction_reject": False,
        "confidence": 1.0,
        "ai_confidence": 1.0,
        "conf": 1.0,
        "reason": reason,
        "gate_reason": reason,
        "ai_reason": reason,
        "detail": reason,
        "lot_multiplier": 1.0,
        "score": s,
    }


def _ai_check(row: dict):
    try:
        orig_ai = _ORIG_AI_CHECK(row)
        hard, reason = _ai_is_hard_reject(orig_ai)
        if hard:
            logger.warning(
                "[TONOSAMA AI BRIDGE] keep NG reason=%s source=%s entry_type=%s symbol=%s side=%s",
                reason,
                row.get("source") if isinstance(row, dict) else None,
                row.get("entry_type") if isinstance(row, dict) else None,
                row.get("symbol") if isinstance(row, dict) else None,
                row.get("side") if isinstance(row, dict) else None,
            )
            return _force_ng(reason, orig_ai)

        if _env_on("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE", True) and _is_tonosama(row):
            side = _su(row.get("entry_decision") or row.get("side"))
            return _force_ok(row, side)
        return orig_ai
    except Exception:
        logger.debug("[TONOSAMA AI BRIDGE] ai check bridge failed", exc_info=True)
        return _ORIG_AI_CHECK(row)


def _pass_check(row: dict, ai: dict, side: str):
    try:
        if _env_on("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE", True) and _is_tonosama(row) and _txt_contains_ok(
            ai.get("reason") if isinstance(ai, dict) else None,
            ai.get("gate_reason") if isinstance(ai, dict) else None,
            ai.get("ai_reason") if isinstance(ai, dict) else None,
            ai.get("detail") if isinstance(ai, dict) else None,
        ):
            s = _score(row, side)
            min_s = _sf(os.getenv("ENTRY_CONTROLLER_TONOSAMA_MIN_SCORE"), 0.01)
            if s < min_s:
                return False, f"TONOSAMA_SCORE_LOW:{s:.3f}<{min_s:.3f}"
            logger.warning(
                "[TONOSAMA AI BRIDGE] accept dedicated ok source=%s entry_type=%s symbol=%s side=%s score=%.4f",
                row.get("source"),
                row.get("entry_type"),
                row.get("symbol"),
                side,
                s,
            )
            return True, f"TONOSAMA_OK score={s:.3f}"

        hard, reason = _ai_is_hard_reject(ai)
        if hard:
            logger.warning(
                "[TONOSAMA AI BRIDGE] reject preserved source=%s entry_type=%s symbol=%s side=%s reason=%s",
                row.get("source") if isinstance(row, dict) else None,
                row.get("entry_type") if isinstance(row, dict) else None,
                row.get("symbol") if isinstance(row, dict) else None,
                side,
                reason,
            )
            return False, reason or "TONOSAMA_HARD_REJECT"

        if _env_on("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE", True) and _is_tonosama(row):
            s = _score(row, side)
            min_s = _sf(os.getenv("ENTRY_CONTROLLER_TONOSAMA_MIN_SCORE"), 0.01)
            if s < min_s:
                return False, f"TONOSAMA_SCORE_LOW:{s:.3f}<{min_s:.3f}"
            logger.warning(
                "[TONOSAMA AI BRIDGE] accept source=%s entry_type=%s symbol=%s side=%s score=%.4f ai_reason=%s",
                row.get("source"),
                row.get("entry_type"),
                row.get("symbol"),
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
        if getattr(cur_pass, "_tonosama_ai_bridge_patch_v3", False):
            _INSTALLED = True
            return True
        _ORIG_AI_CHECK = cur_ai
        _ORIG_PASS_CHECK = cur_pass
        _ai_check._tonosama_ai_bridge_patch_v3 = True  # type: ignore[attr-defined]
        _pass_check._tonosama_ai_bridge_patch_v3 = True  # type: ignore[attr-defined]
        ec.ai_final_entry_check = _ai_check
        ec._passes_ai_gate = _pass_check
        _INSTALLED = True
        logger.warning("[TONOSAMA AI BRIDGE] installed v3 dedicated_ok_whitelist enabled=%s", _env_on("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE", True))
        return True
    except Exception:
        logger.exception("[TONOSAMA AI BRIDGE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA AI BRIDGE] auto install failed")


__all__ = ["install"]
