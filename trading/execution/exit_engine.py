# ============================================================
# File   : trading/execution/exit_engine.py
# Version: Ver1.0-PRODUCTION-EXIT-ENGINE
# ------------------------------------------------------------
# ✔ PUSHベースEXIT
# ✔ 利確
# ✔ 損切り
# ✔ トレーリングストップ
# ✔ MA exit
# ✔ AI exit拡張可能
# ✔ position_manager連携
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from core.global_context.context import global_context as GC

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

TAKE_PROFIT_PCT = 0.05
STOP_LOSS_PCT = -0.03
TRAILING_STOP_PCT = 0.03


# ============================================================
# helpers
# ============================================================

def _get_position_manager():

    pm = GC.get("position_manager")

    if pm is None:

        logger.warning("[EXIT] position_manager missing")

    return pm


# ============================================================
# exit checks
# ============================================================

def _check_take_profit(pos, price):

    pnl_pct = (price - pos.entry_price) / pos.entry_price

    if pnl_pct >= TAKE_PROFIT_PCT:

        return "take_profit"

    return None


def _check_stop_loss(pos, price):

    pnl_pct = (price - pos.entry_price) / pos.entry_price

    if pnl_pct <= STOP_LOSS_PCT:

        return "stop_loss"

    return None


def _check_trailing_stop(pos, price):

    if pos.highest_price <= 0:
        return None

    drop = (pos.highest_price - price) / pos.highest_price

    if drop >= TRAILING_STOP_PCT:

        return "trailing_stop"

    return None


def _check_ma_exit(row):

    close = row.get("close")
    ma5 = row.get("ma5")

    if close is None or ma5 is None:
        return None

    if close < ma5:

        return "ma_exit"

    return None


# ============================================================
# core exit logic
# ============================================================

def evaluate_exit(symbol: str, row: dict):

    pm = _get_position_manager()

    if pm is None:
        return

    pos = pm.get_position(symbol)

    if pos is None or pos.closed:
        return

    price = row.get("close")

    if price is None:
        return

    # update price stats
    pm.update_price(symbol, price)

    # --------------------------------------------------------

    reason = None

    reason = _check_take_profit(pos, price)

    if reason:
        pm.close_position(symbol, price, reason)
        return

    reason = _check_stop_loss(pos, price)

    if reason:
        pm.close_position(symbol, price, reason)
        return

    reason = _check_trailing_stop(pos, price)

    if reason:
        pm.close_position(symbol, price, reason)
        return

    reason = _check_ma_exit(row)

    if reason:
        pm.close_position(symbol, price, reason)
        return


# ============================================================
# batch exit processing
# ============================================================

def process_exit_signals(df: pd.DataFrame):

    if df is None or df.empty:
        return

    required = {"symbol", "close"}

    if not required.issubset(df.columns):

        logger.warning("[EXIT] dataframe missing required columns")

        return

    for _, row in df.iterrows():

        symbol = str(row["symbol"])

        try:

            evaluate_exit(symbol, row)

        except Exception:

            logger.exception(
                "[EXIT] evaluation failed symbol=%s",
                symbol
            )