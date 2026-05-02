# ============================================================
# File   : trading/exit/exit_loop.py
# Version: V69-SPLIT-MAIN-LOOP
# ------------------------------------------------------------
# 【概要】
#   5秒ごとのEXIT監視メインループ。
#
# 【分割後の役割】
#   - open positions を取る
#   - market_state / regime / boost を更新する
#   - 各銘柄を run_exit_for_position() に渡す
#
# 【互換性】
#   - exit_loop = exit_loop_5s を維持
#   - scheduler 側の import を壊さない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

from core.global_context.context import global_context as GC
from trading.exit.exit_position_runner import run_exit_for_position
from trading.exit.exit_utils import get_open_positions_safe
from trading.exit.market_state_builder import build_market_state
from trading.monitor.boost_monitor import BoostMonitor
from trading.risk.boost_engine import BoostEngine

logger = logging.getLogger(__name__)

boost_engine = BoostEngine()
boost_monitor = BoostMonitor()


def _build_regime_safe() -> int:
    try:
        market_state = build_market_state()
        return GC.regime.get_regime(market_state)
    except Exception:
        logger.debug("[EXIT LOOP] regime fallback=2", exc_info=True)
        return 2


def _update_boost_safe(regime: int) -> bool:
    try:
        win_rate = getattr(GC, "recent_win_rate", 0.5)
        drawdown = getattr(GC, "current_drawdown", 0.0)
        collapse_global = getattr(GC, "collapse_prob", 0.0)
        consecutive_losses = getattr(GC, "consecutive_losses", 0)

        boost_active = boost_engine.update(
            win_rate=win_rate,
            regime=regime,
            drawdown=drawdown,
            collapse_prob=collapse_global,
            consecutive_losses=consecutive_losses,
            regime_changed=getattr(GC, "regime_changed", False),
        )

        boost_monitor.update(
            active=boost_active,
            win_rate=win_rate,
            drawdown=drawdown,
            collapse_prob=collapse_global,
            regime=regime,
        )

        return bool(boost_active)

    except Exception:
        logger.exception("[EXIT LOOP] boost update failed")
        return False


def exit_loop_5s() -> None:
    try:
        positions = get_open_positions_safe()
        if not positions:
            return

        now = dt.datetime.now()
        regime = _build_regime_safe()
        boost_active = _update_boost_safe(regime)

        for symbol in sorted(positions.keys()):
            pos = positions[symbol]

            try:
                run_exit_for_position(
                    symbol=symbol,
                    pos=pos,
                    now=now,
                    regime=regime,
                    boost_active=boost_active,
                )
            except Exception:
                logger.exception("[EXIT_LOOP_SYMBOL_ERROR] symbol=%s", symbol)

    except Exception:
        logger.exception("[EXIT_LOOP_FATAL]")


exit_loop = exit_loop_5s


__all__ = [
    "exit_loop",
    "exit_loop_5s",
]