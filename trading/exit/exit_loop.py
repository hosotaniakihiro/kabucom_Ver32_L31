# ============================================================
# File   : trading/exit/exit_loop.py
# Version: V70-BLOWOFF-PROFIT-TAKE-FIRST
# ------------------------------------------------------------
# 【概要】
#   5秒ごとのEXIT監視メインループ。
#
# 【追加】
#   - 株価が吹いたときの利確を通常EXIT判定より前に実行
#   - 100株など小ロットは +0.20% で全利確
#   - 200株以上は +0.25% で一部利確、+0.45% で全利確
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

try:
    from trading.exit.blowoff_profit_take import apply_blowoff_profit_take
except Exception:  # pragma: no cover
    apply_blowoff_profit_take = None

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


def _apply_blowoff_profit_take_safe(symbol: str, pos: dict, regime: int) -> bool:
    if not callable(apply_blowoff_profit_take):
        return False
    try:
        return bool(apply_blowoff_profit_take(symbol=symbol, pos=pos, regime=regime))
    except Exception:
        logger.exception("[EXIT LOOP] blowoff profit take failed symbol=%s", symbol)
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
                # 2026-10-03: 急騰銘柄の空売り (SURGE_SHORT) の建玉は、その戦略の
                #   損切り (+5%) と時間決済 (60 分) だけで扱い、通常の決済ルールは通さない。
                try:
                    from trading.strategies.surge_short import handle_exit as _surge_handle_exit
                    if _surge_handle_exit(symbol, pos, now) is not None:
                        continue
                except Exception:
                    logger.exception("[EXIT LOOP] surge_short handle_exit failed symbol=%s", symbol)
                # 2026-10-06: セリングクライマックスの買い建玉は 損切り 1% / 30 分 で扱う
                try:
                    from trading.strategies.selling_climax import handle_exit as _climax_handle_exit
                    if _climax_handle_exit(symbol, pos, now) is not None:
                        continue
                except Exception:
                    logger.exception("[EXIT LOOP] selling_climax handle_exit failed symbol=%s", symbol)
                # 2026-10-03: 殿様イナゴ由来の買い建玉は 損切り 1.5% / 60 分 で扱う
                try:
                    from trading.strategies.tonosama_exit import handle_exit as _tonosama_handle_exit
                    if _tonosama_handle_exit(symbol, pos, now) is not None:
                        continue
                except Exception:
                    logger.exception("[EXIT LOOP] tonosama_exit handle_exit failed symbol=%s", symbol)

                # 通常のAI/トレーリング/時間EXITより先に、吹き上げ利確を確認する。
                if _apply_blowoff_profit_take_safe(symbol, pos, regime):
                    continue

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
