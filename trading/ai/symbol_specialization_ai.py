# ============================================================
# File   : trading/ai/symbol_specialization_ai.py
# Version: Ver1.0-SYMBOL-SPECIALIZATION-AI-PRODUCTION
# ------------------------------------------------------------
# ✔ 銘柄別AI
# ✔ symbol × strategy 学習
# ✔ 実現損益ベース
# ✔ ranking補正
# ✔ entry filter
# ✔ HFT軽量設計
# ✔ production safe
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

LOOKBACK_DAYS = 40
MIN_SAMPLE = 6


# ============================================================
# CACHE
# ============================================================

_symbol_strategy_stats = {}
_last_update = None


# ============================================================
# LOAD HISTORY
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
# EXPLODE STRATEGY
# ============================================================

def _explode_strategy(df):

    rows = []

    for _, r in df.iterrows():

        flags = str(r.entry_reason).split(",")

        for f in flags:

            f = f.strip()

            if not f:
                continue

            rows.append({

                "symbol": r.symbol,
                "strategy": f,
                "pnl": r.pnl
            })

    return pd.DataFrame(rows)


# ============================================================
# COMPUTE STATS
# ============================================================

def _compute_stats(df):

    stats = {}

    g = df.groupby(["symbol", "strategy"])

    for (symbol, strat), sub in g:

        n = len(sub)

        if n < MIN_SAMPLE:
            continue

        win = (sub.pnl > 0).sum()

        win_rate = win / n

        avg = sub.pnl.mean()

        expectancy = avg * win_rate

        stats[(symbol, strat)] = {

            "trades": n,
            "win_rate": win_rate,
            "expectancy": expectancy
        }

    return stats


# ============================================================
# UPDATE MODEL
# ============================================================

def update_symbol_specialization_model(force=False):

    global _symbol_strategy_stats
    global _last_update

    now = dt.datetime.now()

    if not force:

        if _last_update and (now - _last_update).seconds < 600:
            return

    try:

        df = _load_history()

        if df.empty:
            return

        df2 = _explode_strategy(df)

        stats = _compute_stats(df2)

        _symbol_strategy_stats = stats

        _last_update = now

        logger.info(
            "[SYMBOL AI] updated %s entries",
            len(stats)
        )

    except Exception:

        logger.exception("[SYMBOL AI] update failed")


# ============================================================
# RANKING BOOST
# ============================================================

def symbol_ranking_boost(row):

    symbol = row.get("symbol")

    boost = 0

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:

            s = _symbol_strategy_stats.get((symbol, c))

            if not s:
                continue

            boost += s["expectancy"]

    return boost


# ============================================================
# ENTRY FILTER
# ============================================================

def symbol_entry_filter(row):

    symbol = row.get("symbol")

    bad = 0

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:

            s = _symbol_strategy_stats.get((symbol, c))

            if not s:
                continue

            if s["expectancy"] < 0:

                bad += 1

    if bad >= 2:

        return False

    return True


# ============================================================
# STRATEGY BOOST
# ============================================================

def get_symbol_strategy_multiplier(symbol, flags):

    m = 1.0

    for f in flags:

        s = _symbol_strategy_stats.get((symbol, f))

        if not s:
            continue

        exp = s["expectancy"]

        if exp > 3000:

            m += 0.5

        if exp < -1500:

            m -= 0.4

    m = max(0.5, m)
    m = min(2.0, m)

    return m


# ============================================================
# DEBUG
# ============================================================

def dump_symbol_model():

    for (symbol, strat), v in sorted(
        _symbol_strategy_stats.items(),
        key=lambda x: -x[1]["expectancy"]
    )[:30]:

        logger.info(
            "[SYMBOL AI] %s %s trades=%s exp=%.1f",
            symbol,
            strat,
            v["trades"],
            v["expectancy"]
        )