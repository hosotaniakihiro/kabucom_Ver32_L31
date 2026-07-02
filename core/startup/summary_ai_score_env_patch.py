# ============================================================
# File   : core/startup/summary_ai_score_env_patch.py
# Version: PRODUCTION-STABLE-REV4-SUMMARY-AI-LOW-MOVE-PREFILTER
# ------------------------------------------------------------
# Purpose:
#   - AI.entry_gate の SUMMARY score threshold は MIN_ENTRY_SCORE を参照する。
#   - BUY候補は runner/candidates 側で min_buy=4.0 に絞られている。
#   - entry_controller.py 側の最終BUY閾値が 5.0 のままだと、
#     BUY候補がAI_OKでも BUY_SCORE_LOW / BUY_COMPOSITE_LOW で落ちる。
#   - 起動時に MIN_ENTRY_SCORE と entry_controller BUY閾値を実運用値へ合わせる。
#   - REV3: SUMMARY_AI approved後の blowoff_top 過剰除外と tf=1 history 空を補修する
#           summary_ai_entry_execution_fix_patch を同時に install する。
#   - REV4: LOW_MOVE_RANGE_TOO_SMALL 候補を approved / Top3 前で除外し、
#           snapshot_no_order まで進ませない。
#
# Expected:
#   - BUY score=4.0 の候補が entry_controller 最終AI gate を通過する
#   - SELL候補の score_low:<4.000 も減る
#   - blowoff_top は無効化せず、BUYの本当の吹き上げだけ止める
#   - low-move 候補は発注直前ではなく SUMMARY_AI 選定段階で落ちる
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


def _install_execution_fix_patch() -> dict[str, object]:
    result: dict[str, object] = {"installed": False}
    try:
        from core.startup.summary_ai_entry_execution_fix_patch import install as install_exec_fix

        ok = bool(install_exec_fix())
        result.update({"installed": ok})
    except Exception as e:
        logger.exception("[SUMMARY AI SCORE ENV PATCH] execution fix patch install failed")
        result.update({"error": repr(e)})
    return result


def _install_low_move_prefilter_patch() -> dict[str, object]:
    result: dict[str, object] = {"installed": False}
    try:
        from core.startup.summary_ai_low_move_prefilter_patch import install as install_low_move_prefilter

        ok = bool(install_low_move_prefilter())
        result.update({"installed": ok})
    except Exception as e:
        logger.exception("[SUMMARY AI SCORE ENV PATCH] low-move prefilter patch install failed")
        result.update({"error": repr(e)})
    return result


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

    # low-move 候補は、order_builder の LOW_MOVE_RANGE_TOO_SMALL まで進ませない。
    _set_env_default("SUMMARY_AI_LOW_MOVE_PREFILTER_ENABLED", "1", applied, kept)
    _set_env_default("SUMMARY_AI_PREFILTER_MIN_RANGE_PCT", "0.005", applied, kept)
    _set_env_default("SUMMARY_AI_PREFILTER_REJECT_MISSING_RANGE", "0", applied, kept)

    controller_patch = _patch_entry_controller_thresholds()
    execution_fix_patch = _install_execution_fix_patch()
    low_move_prefilter_patch = _install_low_move_prefilter_patch()

    _INSTALLED = True
    logger.warning(
        "[SUMMARY AI SCORE ENV PATCH] installed applied=%s kept=%s entry_controller=%s execution_fix=%s low_move_prefilter=%s",
        applied,
        kept,
        controller_patch,
        execution_fix_patch,
        low_move_prefilter_patch,
    )


__all__ = ["install_summary_ai_score_env_patch"]
