# ============================================================
# File   : trading/ai/portfolio_risk_ai.py
# Version: Ver1.0-PORTFOLIO-RISK-AI-PRODUCTION
# ------------------------------------------------------------
# ✔ ポートフォリオリスク管理AI
# ✔ 同時ポジション制御
# ✔ 銘柄集中リスク
# ✔ セクター集中防止
# ✔ ボラティリティ調整
# ✔ ドローダウン防御
# ✔ HFT軽量設計
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np
import datetime as dt

from sqlalchemy import text

from database.session import get_position_engine

logger = logging.getLogger(__name__)

# ============================================================
# SETTINGS
# ============================================================

MAX_POSITIONS = 8
MAX_SYMBOL_POSITIONS = 1
MAX_SECTOR_POSITIONS = 3

DAILY_DRAWDOWN_LIMIT = -40000

VOL_HIGH = 40
VOL_MED = 25


# ============================================================
# CACHE
# ============================================================

_open_positions = []
_sector_map = {}
_last_update = None


# ============================================================
# SECTOR MAP (簡易)
# ============================================================

_sector_map = {

    "7011": "defense",
    "7012": "defense",
    "9101": "shipping",
    "9104": "shipping",
    "8035": "semiconductor",
    "6857": "semiconductor",
}


# ============================================================
# LOAD OPEN POSITIONS
# ============================================================

def _load_positions():

    engine = get_position_engine()

    sql = text("""
    SELECT
        symbol,
        pnl,
        entry_time
    FROM positions
    WHERE status='OPEN'
    """)

    df = pd.read_sql(sql, engine)

    return df


# ============================================================
# DAILY PNL
# ============================================================

def _load_daily_pnl():

    engine = get_position_engine()

    today = dt.date.today()

    sql = text("""
    SELECT
        SUM(pnl) as pnl
    FROM trade_history
    WHERE DATE(exit_time)=:today
    """)

    df = pd.read_sql(sql, engine, params={"today": today})

    if df.empty:

        return 0

    return df.iloc[0]["pnl"] or 0


# ============================================================
# UPDATE STATE
# ============================================================

def update_portfolio_state(force=False):

    global _open_positions
    global _last_update

    now = dt.datetime.now()

    if not force:

        if _last_update and (now - _last_update).seconds < 5:

            return

    try:

        df = _load_positions()

        if df is None:

            _open_positions = []

        else:

            _open_positions = df.to_dict("records")

        _last_update = now

    except Exception:

        logger.exception("[PORTFOLIO AI] update failed")


# ============================================================
# POSITION COUNT
# ============================================================

def _count_positions():

    return len(_open_positions)


def _count_symbol(symbol):

    n = 0

    for p in _open_positions:

        if p["symbol"] == symbol:

            n += 1

    return n


def _count_sector(symbol):

    sector = _sector_map.get(symbol)

    if not sector:

        return 0

    n = 0

    for p in _open_positions:

        s = _sector_map.get(p["symbol"])

        if s == sector:

            n += 1

    return n


# ============================================================
# VOLATILITY CHECK
# ============================================================

def _volatility_multiplier(row):

    atr = row.get("atr", 0)

    if atr > VOL_HIGH:

        return 0.4

    if atr > VOL_MED:

        return 0.7

    return 1.0


# ============================================================
# ENTRY CHECK
# ============================================================

def portfolio_entry_allowed(row):

    symbol = row.get("symbol")

    # total positions
    if _count_positions() >= MAX_POSITIONS:

        logger.debug("[PORTFOLIO] max positions")

        return False

    # symbol limit
    if _count_symbol(symbol) >= MAX_SYMBOL_POSITIONS:

        logger.debug("[PORTFOLIO] symbol limit")

        return False

    # sector limit
    if _count_sector(symbol) >= MAX_SECTOR_POSITIONS:

        logger.debug("[PORTFOLIO] sector limit")

        return False

    # drawdown protection
    pnl = _load_daily_pnl()

    if pnl < DAILY_DRAWDOWN_LIMIT:

        logger.warning("[PORTFOLIO] daily drawdown stop")

        return False

    return True


# ============================================================
# LOT ADJUST
# ============================================================

def portfolio_lot_adjust(row, lot):

    mult = _volatility_multiplier(row)

    lot = int(lot * mult)

    lot = max(100, lot)

    return lot


# ============================================================
# DEBUG
# ============================================================

def dump_portfolio():

    logger.info("[PORTFOLIO] open positions %s", len(_open_positions))

    sector_count = {}

    for p in _open_positions:

        s = _sector_map.get(p["symbol"], "unknown")

        sector_count.setdefault(s, 0)

        sector_count[s] += 1

    for s, n in sector_count.items():

        logger.info("[PORTFOLIO] sector %s = %s", s, n)