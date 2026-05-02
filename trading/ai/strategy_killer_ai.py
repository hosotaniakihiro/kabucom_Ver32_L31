# ============================================================
# File   : trading/ai/strategy_killer_ai.py
# Version: Ver1.0-STRATEGY-KILLER-AI-PRODUCTION
# ------------------------------------------------------------
# ✔ 負け戦略自動停止AI
# ✔ strategy expectancy学習
# ✔ drawdown監視
# ✔ entry filter
# ✔ ranking補正
# ✔ production safe
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

LOOKBACK_DAYS = 30
MIN_SAMPLE = 8

KILL_EXPECTANCY = -1500
KILL_DRAWDOWN = -8000


# ============================================================
# CACHE
# ============================================================

_strategy_stats = {}
_killed_strategies = set()

_last_update = None


# ============================================================
# LOAD HISTORY
# ============================================================

def _load_history():

    engine = get_position_engine()

    since = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)

    sql = text("""
    SELECT
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

                "strategy": f,
                "pnl": r.pnl
            })

    return pd.DataFrame(rows)


# ============================================================
# COMPUTE STATS
# ============================================================

def _compute_stats(df):

    stats = {}
    killed = set()

    g = df.groupby("strategy")

    for strat, sub in g:

        n = len(sub)

        if n < MIN_SAMPLE:
            continue

        win = (sub.pnl > 0).sum()

        win_rate = win / n

        avg = sub.pnl.mean()

        expectancy = avg * win_rate

        drawdown = sub.pnl.cumsum().min()

        stats[strat] = {

            "trades": n,
            "win_rate": win_rate,
            "expectancy": expectancy,
            "drawdown": drawdown
        }

        # kill condition
        if expectancy < KILL_EXPECTANCY or drawdown < KILL_DRAWDOWN:

            killed.add(strat)

    return stats, killed


# ============================================================
# UPDATE MODEL
# ============================================================

def update_strategy_killer(force=False):

    global _strategy_stats
    global _killed_strategies
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

        stats, killed = _compute_stats(df2)

        _strategy_stats = stats
        _killed_strategies = killed

        _last_update = now

        logger.info(
            "[STRATEGY AI] killed=%s total=%s",
            len(killed),
            len(stats)
        )

    except Exception:

        logger.exception("[STRATEGY AI] update failed")


# ============================================================
# ENTRY FILTER
# ============================================================

def strategy_killer_filter(row):

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:

            if c in _killed_strategies:

                logger.debug(
                    "[STRATEGY KILL] blocked %s",
                    c
                )

                return False

    return True


# ============================================================
# RANKING PENALTY
# ============================================================

def strategy_penalty(row):

    penalty = 0

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:

            if c in _killed_strategies:

                penalty -= 1000

    return penalty


# ============================================================
# DEBUG
# ============================================================

def dump_strategy_killer():

    for s in sorted(_killed_strategies):

        st = _strategy_stats.get(s)

        if not st:
            continue

        logger.info(
            "[STRATEGY KILLED] %s trades=%s exp=%.1f dd=%.1f",
            s,
            st["trades"],
            st["expectancy"],
            st["drawdown"]
        )