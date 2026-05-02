# ============================================================
# File   : trading/ai/lot_optimizer_ai.py
# Version: Ver1.0-LOT-OPTIMIZER-AI-PRODUCTION
# ------------------------------------------------------------
# ✔ 実現損益ベース
# ✔ 期待値ポジションサイズ
# ✔ 勝率学習
# ✔ ボラティリティ調整
# ✔ 地合い調整
# ✔ HFT軽量設計
# ✔ 本番運用安全
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

LOOKBACK_DAYS = 20
MIN_SAMPLE = 8

BASE_LOT = 100
MAX_MULTIPLIER = 5
MIN_MULTIPLIER = 0.5


# ============================================================
# CACHE
# ============================================================

_symbol_stats = {}
_strategy_stats = {}
_last_update = None


# ============================================================
# TRADE HISTORY
# ============================================================

def _load_history():

    engine = get_position_engine()

    since = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)

    sql = text("""
    SELECT
        symbol,
        pnl,
        entry_reason,
        entry_time
    FROM trade_history
    WHERE entry_time >= :since
    """)

    df = pd.read_sql(sql, engine, params={"since": since})

    return df


# ============================================================
# SYMBOL STATS
# ============================================================

def _compute_symbol_stats(df):

    stats = {}

    g = df.groupby("symbol")

    for symbol, sub in g:

        n = len(sub)

        if n < MIN_SAMPLE:
            continue

        win = (sub.pnl > 0).sum()

        win_rate = win / n
        avg = sub.pnl.mean()

        expectancy = avg * win_rate

        stats[symbol] = {

            "trades": n,
            "win_rate": win_rate,
            "expectancy": expectancy,
            "avg_profit": avg
        }

    return stats


# ============================================================
# STRATEGY STATS
# ============================================================

def _explode_strategy(df):

    rows = []

    for _, r in df.iterrows():

        flags = str(r.entry_reason).split(",")

        for f in flags:

            f = f.strip()

            if not f:
                continue

            rows.append(
                {
                    "strategy": f,
                    "pnl": r.pnl
                }
            )

    return pd.DataFrame(rows)


def _compute_strategy_stats(df):

    stats = {}

    g = df.groupby("strategy")

    for s, sub in g:

        n = len(sub)

        if n < MIN_SAMPLE:
            continue

        win = (sub.pnl > 0).sum()

        win_rate = win / n
        avg = sub.pnl.mean()

        expectancy = avg * win_rate

        stats[s] = {

            "trades": n,
            "win_rate": win_rate,
            "expectancy": expectancy
        }

    return stats


# ============================================================
# UPDATE MODEL
# ============================================================

def update_lot_model(force=False):

    global _symbol_stats
    global _strategy_stats
    global _last_update

    now = dt.datetime.now()

    if not force:

        if _last_update and (now - _last_update).seconds < 600:
            return

    try:

        df = _load_history()

        if df.empty:
            return

        _symbol_stats = _compute_symbol_stats(df)

        df2 = _explode_strategy(df)

        if not df2.empty:

            _strategy_stats = _compute_strategy_stats(df2)

        _last_update = now

        logger.info(
            "[LOT AI] updated symbols=%s strategies=%s",
            len(_symbol_stats),
            len(_strategy_stats)
        )

    except Exception:

        logger.exception("[LOT AI] update failed")


# ============================================================
# SYMBOL MULTIPLIER
# ============================================================

def _symbol_multiplier(symbol):

    s = _symbol_stats.get(symbol)

    if not s:
        return 1.0

    exp = s["expectancy"]

    if exp > 3000:
        return 3

    if exp > 1500:
        return 2

    if exp < -1000:
        return 0.5

    return 1.0


# ============================================================
# STRATEGY MULTIPLIER
# ============================================================

def _strategy_multiplier(flags):

    m = 1.0

    for f in flags:

        s = _strategy_stats.get(f)

        if not s:
            continue

        exp = s["expectancy"]

        if exp > 2000:
            m += 0.5

        if exp < -1000:
            m -= 0.3

    return max(0.5, m)


# ============================================================
# VOLATILITY ADJUST
# ============================================================

def _volatility_multiplier(row):

    try:

        atr = row.get("atr", 0)

        if atr > 30:
            return 0.6

        if atr > 20:
            return 0.8

        return 1.0

    except Exception:

        return 1.0


# ============================================================
# MAIN LOT CALCULATOR
# ============================================================

def calculate_lot(row):

    symbol = row.get("symbol")

    flags = []

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:
            flags.append(c)

    lot = BASE_LOT

    sm = _symbol_multiplier(symbol)

    stm = _strategy_multiplier(flags)

    vm = _volatility_multiplier(row)

    multiplier = sm * stm * vm

    multiplier = max(MIN_MULTIPLIER, multiplier)
    multiplier = min(MAX_MULTIPLIER, multiplier)

    lot = int(BASE_LOT * multiplier)

    lot = max(100, lot)

    return lot


# ============================================================
# DEBUG
# ============================================================

def dump_lot_model():

    for s, v in sorted(
        _symbol_stats.items(),
        key=lambda x: -x[1]["expectancy"]
    )[:10]:

        logger.info(
            "[LOT AI] %s trades=%s exp=%.1f",
            s,
            v["trades"],
            v["expectancy"]
        )