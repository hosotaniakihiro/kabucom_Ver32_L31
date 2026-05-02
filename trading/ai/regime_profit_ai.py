# ============================================================
# File   : trading/ai/regime_profit_ai.py
# Version: Ver1.0-REGIME-PROFIT-AI-PRODUCTION
# ------------------------------------------------------------
# ✔ 地合い別利益AI
# ✔ strategy × market regime
# ✔ 実現損益ベース学習
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

LOOKBACK_DAYS = 30
MIN_SAMPLE = 6


# ============================================================
# CACHE
# ============================================================

_regime_strategy_stats = {}
_last_update = None


# ============================================================
# MARKET REGIME
# ============================================================

def detect_market_regime(df: pd.DataFrame):

    try:

        close = df["close"]

        ma25 = close.rolling(25).mean()
        ma75 = close.rolling(75).mean()

        if ma25.iloc[-1] > ma75.iloc[-1]:

            if close.iloc[-1] > ma25.iloc[-1]:

                return "BULL"

            return "BULL_PULLBACK"

        if ma25.iloc[-1] < ma75.iloc[-1]:

            if close.iloc[-1] < ma25.iloc[-1]:

                return "BEAR"

            return "BEAR_REBOUND"

        return "RANGE"

    except Exception:

        return "UNKNOWN"


# ============================================================
# LOAD TRADE HISTORY
# ============================================================

def _load_history():

    engine = get_position_engine()

    since = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)

    sql = text("""
    SELECT
        symbol,
        pnl,
        entry_reason,
        entry_time,
        regime
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

                "regime": r.regime,
                "strategy": f,
                "pnl": r.pnl
            })

    return pd.DataFrame(rows)


# ============================================================
# COMPUTE STATS
# ============================================================

def _compute_stats(df):

    stats = {}

    g = df.groupby(["regime", "strategy"])

    for (regime, strat), sub in g:

        n = len(sub)

        if n < MIN_SAMPLE:
            continue

        win = (sub.pnl > 0).sum()

        win_rate = win / n

        avg = sub.pnl.mean()

        expectancy = avg * win_rate

        stats[(regime, strat)] = {

            "trades": n,
            "win_rate": win_rate,
            "expectancy": expectancy
        }

    return stats


# ============================================================
# UPDATE MODEL
# ============================================================

def update_regime_profit_model(force=False):

    global _regime_strategy_stats
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

        _regime_strategy_stats = stats

        _last_update = now

        logger.info(
            "[REGIME AI] updated %s entries",
            len(stats)
        )

    except Exception:

        logger.exception("[REGIME AI] update failed")


# ============================================================
# GET CURRENT REGIME
# ============================================================

def get_current_regime(market_df):

    return detect_market_regime(market_df)


# ============================================================
# RANKING BOOST
# ============================================================

def regime_ranking_boost(row, regime):

    boost = 0

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:

            s = _regime_strategy_stats.get((regime, c))

            if not s:
                continue

            boost += s["expectancy"]

    return boost


# ============================================================
# ENTRY FILTER
# ============================================================

def regime_entry_filter(row, regime):

    bad = 0

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:

            s = _regime_strategy_stats.get((regime, c))

            if not s:
                continue

            if s["expectancy"] < 0:

                bad += 1

    if bad >= 2:

        return False

    return True


# ============================================================
# DEBUG
# ============================================================

def dump_regime_model():

    for (regime, strat), v in sorted(
        _regime_strategy_stats.items(),
        key=lambda x: -x[1]["expectancy"]
    )[:20]:

        logger.info(
            "[REGIME AI] %s %s trades=%s exp=%.1f",
            regime,
            strat,
            v["trades"],
            v["expectancy"]
        )