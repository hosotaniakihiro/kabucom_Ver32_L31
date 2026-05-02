# ============================================================
# File   : trading/execution/execution_engine.py
# Version: Ver1.0-PRODUCTION-STABLE
# ------------------------------------------------------------
# ✔ trading_engine 接続
# ✔ RL execution agent 対応
# ✔ kabu API 接続
# ✔ 成行 / 指値
# ✔ 重複発注防止
# ✔ fail safe
# ✔ singleton
# ============================================================

from __future__ import annotations

import logging
import threading
from typing import Optional, Dict

from kabu_api.buy_sell_entry import buy_entry, sell_entry

logger = logging.getLogger(__name__)

# ============================================================
# OPTIONAL RL EXECUTION
# ============================================================

try:
    from trading.ai.rl_execution_agent import get_rl_execution_agent
    RL_AVAILABLE = True
except Exception:
    RL_AVAILABLE = False


# ============================================================
# EXECUTION ENGINE
# ============================================================

class ExecutionEngine:

    def __init__(self):

        self.lock = threading.Lock()

        self.last_order = {}

        if RL_AVAILABLE:
            self.rl_agent = get_rl_execution_agent()
        else:
            self.rl_agent = None

        logger.info("[EXECUTION ENGINE] initialized")

    # ========================================================
    # MAIN EXECUTION
    # ========================================================

    def execute(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: Optional[float] = None,
        context: Optional[Dict] = None
    ) -> Dict:

        with self.lock:

            try:

                if not symbol:
                    raise ValueError("symbol empty")

                side = side.upper()

                if side not in ("BUY", "SELL"):
                    raise ValueError("invalid side")

                # ---------------------------------
                # RL execution decision
                # ---------------------------------

                if self.rl_agent:

                    try:

                        action = self.rl_agent.decide_execution(
                            symbol=symbol,
                            side=side,
                            qty=qty,
                            price=price,
                            context=context
                        )

                        if action:

                            qty = action.get("qty", qty)
                            price = action.get("price", price)

                    except Exception:

                        logger.exception("[RL EXECUTION] failed")

                # ---------------------------------
                # duplicate guard
                # ---------------------------------

                last = self.last_order.get(symbol)

                if last and last == side:

                    logger.warning(
                        "[EXECUTION] duplicate blocked %s %s",
                        symbol,
                        side
                    )

                    return {
                        "status": "BLOCKED_DUPLICATE"
                    }

                # ---------------------------------
                # order send
                # ---------------------------------

                if side == "BUY":

                    result = buy_entry(
                        symbol=symbol,
                        qty=qty,
                        price=price
                    )

                else:

                    result = sell_entry(
                        symbol=symbol,
                        qty=qty,
                        price=price
                    )

                self.last_order[symbol] = side

                logger.info(
                    "[EXECUTION] %s %s qty=%s price=%s",
                    side,
                    symbol,
                    qty,
                    price
                )

                return {
                    "status": "ORDER_SENT",
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "result": result
                }

            except Exception:

                logger.exception("[EXECUTION ENGINE] order failed")

                return {
                    "status": "ERROR"
                }

    # ========================================================
    # BUY
    # ========================================================

    def buy(
        self,
        symbol: str,
        qty: int,
        price: Optional[float] = None,
        context: Optional[Dict] = None
    ):

        return self.execute(
            symbol=symbol,
            side="BUY",
            qty=qty,
            price=price,
            context=context
        )

    # ========================================================
    # SELL
    # ========================================================

    def sell(
        self,
        symbol: str,
        qty: int,
        price: Optional[float] = None,
        context: Optional[Dict] = None
    ):

        return self.execute(
            symbol=symbol,
            side="SELL",
            qty=qty,
            price=price,
            context=context
        )


# ============================================================
# SINGLETON
# ============================================================

_engine = None


def get_execution_engine() -> ExecutionEngine:

    global _engine

    if _engine is None:

        _engine = ExecutionEngine()

    return _engine