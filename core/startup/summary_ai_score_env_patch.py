# ============================================================
# File   : core/startup/summary_ai_score_env_patch.py
# Version: PRODUCTION-STABLE-REV3-SUMMARY-AI-ENTRY-BUY-THRESHOLD-PATCH
# ------------------------------------------------------------
# Purpose:
#   - AI.entry_gate の SUMMARY score threshold は MIN_ENTRY_SCORE を参照する。
#   - BUY候補は runner/candidates 側で min_buy=4.0 に絞られている。
#   - entry_controller.py 側の最終BUY閾値が 5.0 のままだと、
#     BUY候補がAI_OKでも BUY_SCORE_LOW / BUY_COMPOSITE_LOW で落ちる。
#   - 起動時に MIN_ENTRY_SCORE と entry_controller BUY閾値を実運用値へ合わせる。
#   - REV3: SUMMARY_AI 1分足だけ LOW_MOVE_RANGE_TOO_SMALL を 1円以上かつ
#     0.03%以上で再判定する補助パッチを同時に install する。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSTALLED = False


def _blank(v: object) -> bool:
    try:
        return v is None or str(v).strip() == ""
    except Exception:
        return True


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if _blank(v):
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _set_env_default(key: str, value: str, applied: dict[str, str], kept: dict[str, str]) -> None:
    cur = os.environ.get(key)
    if _blank(cur):
        os.environ[key] = value
        applied[key] = value
    else:
        kept[key] = str(cur)


def _patch_entry_controller_thresholds() -> dict[str, object]:
    """
    entry_controller は起動時点で import 済みのことが多いので、
    os.environ だけでなくモジュール定数も直接上書きする。
    """
    result: dict[str, object] = {"patched": False}
    try:
        import trading.handlers.entry_controller as ec

        min_buy_score = _env_float("ENTRY_CONTROLLER_MIN_SUMMARY_SCORE_BUY", 3.0)
        min_buy_comp = _env_float("ENTRY_CONTROLLER_MIN_COMPOSITE_SCORE_BUY", 3.0)
        min_buy_conf = _env_float("ENTRY_CONTROLLER_MIN_AI_CONFIDENCE_BUY", 0.60)

        old = {
            "MIN_SUMMARY_SCORE_BUY": getattr(ec, "MIN_SUMMARY_SCORE_BUY", None),
            "MIN_COMPOSITE_SCORE_BUY": getattr(ec, "MIN_COMPOSITE_SCORE_BUY", None),
            "MIN_AI_CONFIDENCE_BUY": getattr(ec, "MIN_AI_CONFIDENCE_BUY", None),
        }

        ec.MIN_SUMMARY_SCORE_BUY = float(min_buy_score)
        ec.MIN_COMPOSITE_SCORE_BUY = float(min_buy_comp)
        ec.MIN_AI_CONFIDENCE_BUY = float(min_buy_conf)

        result.update(
            {
                "patched": True,
                "old": old,
                "new": {
                    "MIN_SUMMARY_SCORE_BUY": ec.MIN_SUMMARY_SCORE_BUY,
                    "MIN_COMPOSITE_SCORE_BUY": ec.MIN_COMPOSITE_SCORE_BUY,
                    "MIN_AI_CONFIDENCE_BUY": ec.MIN_AI_CONFIDENCE_BUY,
                },
            }
        )
    except Exception as e:
        logger.exception("[SUMMARY AI SCORE ENV PATCH] entry_controller threshold patch failed")
        result.update({"error": repr(e)})
    return result


def _install_summary_ai_1m_range_relax() -> bool:
    try:
        from core.startup.summary_ai_1m_range_relax_patch import install as _install
        return bool(_install())
    except Exception as e:
        logger.exception("[SUMMARY AI SCORE ENV PATCH] SUMMARY_AI 1m range relax install failed")
        return False


def install_summary_ai_score_env_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    applied: dict[str, str] = {}
    kept: dict[str, str] = {}

    _set_env_default("MIN_ENTRY_SCORE", "3.0", applied, kept)
    _set_env_default("ENTRY_CONTROLLER_MIN_SUMMARY_SCORE_BUY", "3.0", applied, kept)
    _set_env_default("ENTRY_CONTROLLER_MIN_COMPOSITE_SCORE_BUY", "3.0", applied, kept)
    _set_env_default("ENTRY_CONTROLLER_MIN_AI_CONFIDENCE_BUY", "0.60", applied, kept)
    _set_env_default("SUMMARY_AI_1M_MIN_RANGE_PCT", "0.0003", applied, kept)
    _set_env_default("SUMMARY_AI_1M_MIN_RANGE_VALUE", "1.0", applied, kept)

    controller_patch = _patch_entry_controller_thresholds()
    range_relax = _install_summary_ai_1m_range_relax()

    _INSTALLED = True
    logger.warning(
        "[SUMMARY AI SCORE ENV PATCH] installed applied=%s kept=%s entry_controller=%s range_relax=%s",
        applied,
        kept,
        controller_patch,
        range_relax,
    )


__all__ = ["install_summary_ai_score_env_patch"]
