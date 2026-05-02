# ============================================================
# File   : trading/exit/exit_engine.py
# Version: Ver1.0-PRODUCTION-EXIT-ENGINE-FULL
# ------------------------------------------------------------
# ✔ take profit
# ✔ stop loss
# ✔ trailing stop
# ✔ time exit
# ✔ trend breakdown exit
# ✔ safe guard
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from typing import Optional, Dict, Any

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# config（調整可能）
# ============================================================

TAKE_PROFIT = 0.03        # +3%
STOP_LOSS = -0.015        # -1.5%
TRAILING_STOP = 0.02      # 2%戻し
MAX_HOLD_MINUTES = 30     # 最大保有時間


# ============================================================
# helpers
# ============================================================

def _safe(v, default=0.0):
    try:
        if v is None:
            return default
        v = float(v)
        if np.isnan(v) or np.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# pnl
# ============================================================

def _calc_pnl(entry_price, current_price):
    if entry_price <= 0:
        return 0.0
    return (current_price - entry_price) / entry_price


# ============================================================
# main
# ============================================================

def check_exit(
    position: Dict[str, Any],
    market: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """
    position:
        entry_price
        entry_time
        max_price

    market:
        price
        trend
        momentum
    """

    try:

        now = dt.datetime.now()

        entry_price = _safe(position.get("entry_price"))
        entry_time = position.get("entry_time")
        max_price = _safe(position.get("max_price", entry_price))

        current_price = _safe(market.get("price"))

        trend = _safe(market.get("_score_trend"))
        momentum = _safe(market.get("_score_momentum"))

        if current_price <= 0:
            return None

        # ----------------------------------------------------
        # PnL
        # ----------------------------------------------------
        pnl = _calc_pnl(entry_price, current_price)

        # ----------------------------------------------------
        # max price 更新（外でもやってOK）
        # ----------------------------------------------------
        if current_price > max_price:
            position["max_price"] = current_price
            max_price = current_price

        # ----------------------------------------------------
        # ① 損切（最優先）
        # ----------------------------------------------------
        if pnl <= STOP_LOSS:
            return {
                "action": "STOP_LOSS",
                "price": current_price,
                "pnl": pnl,
            }

        # ----------------------------------------------------
        # ② 利確
        # ----------------------------------------------------
        if pnl >= TAKE_PROFIT:
            return {
                "action": "TAKE_PROFIT",
                "price": current_price,
                "pnl": pnl,
            }

        # ----------------------------------------------------
        # ③ トレーリング（爆益用）
        # ----------------------------------------------------
        drawdown = (max_price - current_price) / max_price

        if drawdown >= TRAILING_STOP:
            return {
                "action": "TRAILING_EXIT",
                "price": current_price,
                "pnl": pnl,
            }

        # ----------------------------------------------------
        # ④ トレンド崩壊
        # ----------------------------------------------------
        if trend < -0.3 and momentum < 0:
            return {
                "action": "TREND_BREAK",
                "price": current_price,
                "pnl": pnl,
            }

        # ----------------------------------------------------
        # ⑤ 時間切れ
        # ----------------------------------------------------
        if entry_time:
            hold_time = (now - entry_time).total_seconds() / 60.0

            if hold_time >= MAX_HOLD_MINUTES:
                return {
                    "action": "TIME_EXIT",
                    "price": current_price,
                    "pnl": pnl,
                }

        return None

    except Exception:
        logger.exception("[exit_engine] failed")
        return None