# ============================================================
# File   : trading/exit/exit_finalize.py
# Version: V1.1-TRADE-GUARD-EXIT-RECORD
# ------------------------------------------------------------
# 【概要】
#   EXIT実行と学習更新。
#
# 【追加】
#   - EXIT成功時/試行時の銘柄別成績を trade_guard に記録。
#   - 損切り後15分クールダウン、2連敗当日停止、全利確後5分停止に接続。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

from core.global_context.context import global_context as GC
from trading.ai.rl.reward_normalizer import normalize_reward
from trading.exit.executor import execute_exit
from trading.exit.exit_utils import safe_float
from trading.exit.symbol_trade_guard import record_exit_event

logger = logging.getLogger(__name__)


def finalize_exit(
    symbol: str,
    price: float,
    reason: str,
    cluster_id: int,
    regime: int,
    inago_state: int,
    pnl: float,
    collapse_prob: float = 0.0,
    ctx: Any = None,
    rl_state: Any = None,
) -> None:
    try:
        pnl = safe_float(pnl)
        collapse_prob = safe_float(collapse_prob)

        logger.warning(
            "[EXIT EXECUTE] symbol=%s reason=%s price=%.4f pnl=%.4f",
            symbol,
            reason,
            price,
            pnl,
        )

        execute_ok = False
        try:
            execute_ok = bool(execute_exit(symbol, reason, price))
        except Exception:
            logger.exception(
                "[EXECUTE_EXIT_ERROR] symbol=%s reason=%s price=%.4f",
                symbol,
                reason,
                price,
            )

        if execute_ok:
            try:
                record_exit_event(symbol, pnl=pnl, reason=reason)
            except Exception:
                logger.exception("[TRADE_GUARD_RECORD_EXIT_ERROR] symbol=%s", symbol)
        else:
            logger.warning(
                "[EXIT EXECUTE] not recorded to trade_guard because execute_exit failed symbol=%s reason=%s",
                symbol,
                reason,
            )

        try:
            if hasattr(GC, "bandit"):
                GC.bandit.update(cluster_id, regime, inago_state, pnl)
        except Exception:
            logger.exception("[BANDIT_UPDATE_ERROR]")

        if rl_state is not None:
            try:
                if not hasattr(GC, "ai") or not GC.ai:
                    return

                rl_agent = GC.ai.get_rl_agent()
                if not rl_agent:
                    return

                atr = getattr(ctx, "atr_1min", 0.0) if ctx else 0.0
                reward = normalize_reward(
                    pnl=pnl,
                    atr=atr,
                    regime=regime,
                    inago_state=inago_state,
                    collapse_strength=collapse_prob,
                )
                next_state = rl_agent.encode_state(regime, cluster_id, inago_state, 0.0)
                rl_agent.update(rl_state, "EXIT", reward, next_state)

            except Exception:
                logger.exception("[RL_UPDATE_ERROR]")

    except Exception:
        logger.exception("[FINALIZE_EXIT_ERROR]")


__all__ = ["finalize_exit"]
