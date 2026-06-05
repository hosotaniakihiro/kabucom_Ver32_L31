# ============================================================
# File   : core/startup/ranking_entry_gate_failopen_patch.py
# Version: V1-RANKING-MTF-MODEL-FAILOPEN
# ------------------------------------------------------------
# Purpose:
#   RANKING候補がprecheck/流動性/ATRを通過しているのに、
#     AI\model\ranking_entry_lgbm.pkl が未配置
#     ai_reason=mtf_low
#   だけで全スキップされる問題を防ぐ。
#
# 方針:
#   - SUMMARYは従来通り厳格。
#   - RANKINGのみ、score/turnover/volume が十分なら mtf_low / model missing を fail-open。
#   - entry_controller は ai_final_entry_check を直接importしているため、
#     AI.entry_gate と trading.handlers.entry_controller の両方を差し替える。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm_source(row: dict) -> str:
    return str(row.get("source") or row.get("pipeline_source") or "").strip().upper()


def _norm_side(row: dict) -> str:
    return str(row.get("side") or row.get("entry_decision") or "").strip().upper()


def _ranking_score(row: dict) -> float:
    vals = [
        row.get("score"),
        row.get("final_score"),
        row.get("display_score"),
        row.get("score_total"),
        row.get("total_score"),
        row.get("score_buy"),
        row.get("buy_score"),
        row.get("score_sell"),
        row.get("sell_score"),
    ]
    return max(abs(_safe_float(v, 0.0)) for v in vals)


def _turnover(row: dict) -> float:
    t = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    if t <= 0:
        price = _safe_float(row.get("close") or row.get("close_price") or row.get("price") or row.get("current_price"), 0.0)
        vol = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
        if price > 0 and vol > 0:
            t = price * vol
    return t


def _fallback_allowed(row: dict, ret: dict | None = None) -> tuple[bool, dict[str, Any]]:
    if not _env_bool("RANKING_AI_GATE_FAILOPEN_ENABLED", True):
        return False, {"disabled": True}
    if _norm_source(row) != "RANKING":
        return False, {"source": _norm_source(row)}

    score = _ranking_score(row)
    turnover = _turnover(row)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    min_score = _env_float("RANKING_AI_GATE_FAILOPEN_MIN_SCORE", 50.0)
    min_turnover = _env_float("RANKING_AI_GATE_FAILOPEN_MIN_TURNOVER", 50_000_000.0)
    min_volume = _env_float("RANKING_AI_GATE_FAILOPEN_MIN_VOLUME", 30_000.0)
    reason = str((ret or {}).get("reason") or row.get("ai_reason") or "")

    ok_reason = ("mtf_low" in reason) or ("ranking entry model not found" in reason) or reason in {"", "NONE"}
    ok = score >= min_score and turnover >= min_turnover and volume >= min_volume and ok_reason
    return ok, {
        "score": score,
        "turnover": turnover,
        "volume": volume,
        "min_score": min_score,
        "min_turnover": min_turnover,
        "min_volume": min_volume,
        "reason": reason,
        "ok_reason": ok_reason,
    }


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import AI.entry_gate as eg
    except Exception:
        logger.debug("[RANKING AI GATE FAILOPEN] AI.entry_gate not ready", exc_info=True)
        return False

    try:
        old = getattr(eg, "ai_final_entry_check", None)
        if not callable(old):
            return False
        if getattr(old, "_ranking_ai_gate_failopen_v1", False):
            _INSTALLED = True
            return True

        def _patched_ai_final_entry_check(row: dict) -> dict:
            ret = old(row)
            try:
                if isinstance(ret, dict) and ret.get("allow"):
                    return ret
                ok, detail = _fallback_allowed(row if isinstance(row, dict) else {}, ret if isinstance(ret, dict) else None)
                if ok:
                    conf = max(0.62, min(1.20, detail["score"] / 100.0))
                    logger.warning(
                        "[RANKING AI GATE FAILOPEN] allow symbol=%s side=%s detail=%s original=%s",
                        (row or {}).get("symbol"),
                        _norm_side(row or {}),
                        detail,
                        ret,
                    )
                    return {
                        "allow": True,
                        "confidence": conf,
                        "lot_multiplier": max(0.5, min(1.5, 0.5 + conf)),
                        "reason": "RANKING_AI_GATE_FAILOPEN|score={score:.2f}|turnover={turnover:.0f}|reason={reason}".format(**detail),
                        "model_used": "RANKING_SCORE_FAILOPEN",
                    }
                logger.info(
                    "[RANKING AI GATE FAILOPEN] keep block symbol=%s side=%s detail=%s original=%s",
                    (row or {}).get("symbol"),
                    _norm_side(row or {}),
                    detail,
                    ret,
                )
            except Exception:
                logger.exception("[RANKING AI GATE FAILOPEN] wrapper failed")
            return ret

        _patched_ai_final_entry_check._ranking_ai_gate_failopen_v1 = True  # type: ignore[attr-defined]
        _patched_ai_final_entry_check._original = old  # type: ignore[attr-defined]
        eg.ai_final_entry_check = _patched_ai_final_entry_check

        # entry_controller は関数を直接importしているので、こちらも差し替える。
        try:
            import trading.handlers.entry_controller as ec
            ec.ai_final_entry_check = _patched_ai_final_entry_check
        except Exception:
            logger.debug("[RANKING AI GATE FAILOPEN] entry_controller patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[RANKING AI GATE FAILOPEN] installed v1 min_score=%s min_turnover=%s min_volume=%s",
            os.getenv("RANKING_AI_GATE_FAILOPEN_MIN_SCORE", "50.0"),
            os.getenv("RANKING_AI_GATE_FAILOPEN_MIN_TURNOVER", "50000000"),
            os.getenv("RANKING_AI_GATE_FAILOPEN_MIN_VOLUME", "30000"),
        )
        return True
    except Exception:
        logger.exception("[RANKING AI GATE FAILOPEN] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING AI GATE FAILOPEN] auto install failed")


__all__ = ["install"]
