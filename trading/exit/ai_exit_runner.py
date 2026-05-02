# ============================================================
# File   : trading/exit/ai_exit_runner.py
# Version: V1.0-SPLIT-AI-EXIT-RUNNER
# ------------------------------------------------------------
# 【概要】
#   AI EXIT 判定ランナー。
#
# 【役割】
#   - ai_exit_decision() 呼び出し
#   - allow_exit=True の場合 finalize_exit()
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict

from trading.exit.exit_finalize import finalize_exit
from trading.exit.exit_utils import safe_bool, safe_float

logger = logging.getLogger(__name__)

try:
    from trading.exit.ai_exit_gate import ai_exit_decision
except Exception:
    ai_exit_decision = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


AI_EXIT_ENABLED = _env_bool("AI_EXIT_ENABLED", True)
AI_EXIT_ACTIVE_MODE = _env_bool("AI_EXIT_ACTIVE_MODE", True)
AI_EXIT_DRY_RUN = _env_bool("AI_EXIT_DRY_RUN", True)


def apply_ai_exit_if_needed(
    *,
    symbol: str,
    side: str,
    price: float,
    entry_price: float,
    pnl: float,
    features: Dict[str, Any],
    ctx: Any,
    now: dt.datetime,
    cluster_id: int,
    regime: int,
    inago_state: int,
    collapse_prob: float,
) -> bool:
    if not AI_EXIT_ENABLED:
        return False

    if not AI_EXIT_ACTIVE_MODE:
        return False

    if ai_exit_decision is None:
        return False

    try:
        holding_seconds = 0
        if hasattr(ctx, "holding_seconds"):
            holding_seconds = ctx.holding_seconds(now)

        ai_decision = ai_exit_decision(
            symbol=symbol,
            side=side,
            pnl=pnl,
            features=features,
            price=price,
            entry_price=entry_price,
            holding_seconds=holding_seconds,
        )

        allow_exit = bool(ai_decision.get("allow_exit"))
        confidence = safe_float(ai_decision.get("confidence"))
        ai_reason = ai_decision.get("reason") or "AI_EXIT"
        exit_type = ai_decision.get("exit_type") or "AI_EXIT"

        daily_score = safe_float(features.get("daily_score"))
        daily_sell_score = safe_float(features.get("daily_sell_score"))
        daily_exit_warn = safe_bool(features.get("daily_exit_warn"))

        if allow_exit:
            logger.warning(
                "[AI_EXIT_OK] symbol=%s side=%s price=%.4f entry=%.4f "
                "pnl=%.4f conf=%.3f type=%s "
                "daily=%.2f daily_sell=%.2f daily_warn=%s "
                "reason=%s dry_run=%s",
                symbol,
                side,
                price,
                entry_price,
                pnl,
                confidence,
                exit_type,
                daily_score,
                daily_sell_score,
                daily_exit_warn,
                ai_reason,
                AI_EXIT_DRY_RUN,
            )

            if AI_EXIT_DRY_RUN:
                logger.warning(
                    "[AI_EXIT_DRY_RUN] would execute exit symbol=%s price=%.4f "
                    "daily=%.2f reason=%s",
                    symbol,
                    price,
                    daily_score,
                    ai_reason,
                )
                return True

            finalize_exit(
                symbol=symbol,
                price=price,
                reason=f"{exit_type}:{ai_reason}",
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
            )
            return True

        logger.info(
            "[AI_EXIT_HOLD] symbol=%s pnl=%.4f conf=%.3f "
            "daily=%.2f daily_sell=%.2f daily_warn=%s reason=%s",
            symbol,
            pnl,
            confidence,
            daily_score,
            daily_sell_score,
            daily_exit_warn,
            ai_reason,
        )

        return False

    except Exception:
        logger.exception("[AI_ACTIVE_EXIT_ERROR] symbol=%s", symbol)
        return False


__all__ = [
    "apply_ai_exit_if_needed",
]