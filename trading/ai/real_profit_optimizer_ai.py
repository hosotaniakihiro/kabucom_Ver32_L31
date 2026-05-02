# ============================================================
# File   : trading/ai/real_profit_optimizer_ai.py
# Version: Ver1.0-PROFIT-LEARNING-ENGINE-PRODUCTION
# ------------------------------------------------------------
# ✔ 実現損益ベース AI
# ✔ 勝率学習
# ✔ flagごとの収益期待値計算
# ✔ entry条件自動最適化
# ✔ ranking補正
# ✔ self evolving strategy
# ✔ HFT対応軽量設計
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
# GLOBAL
# ============================================================

_profit_stats_cache = {}
_last_update = None


# ============================================================
# 設定
# ============================================================

LOOKBACK_DAYS = 20
MIN_SAMPLE = 8


# ============================================================
# DB LOAD
# ============================================================

def _load_trade_history():

    engine = get_position_engine()

    since = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)

    sql = text("""
    SELECT
        symbol,
        entry_time,
        exit_time,
        pnl,
        score,
        entry_reason
    FROM trade_history
    WHERE entry_time >= :since
    """)

    df = pd.read_sql(sql, engine, params={"since": since})

    return df


# ============================================================
# ENTRY FLAG PARSE
# ============================================================

def _explode_flags(df: pd.DataFrame):

    rows = []

    for _, r in df.iterrows():

        flags = str(r.get("entry_reason", "")).split(",")

        for f in flags:

            f = f.strip()

            if not f:
                continue

            rows.append(
                {
                    "flag": f,
                    "pnl": r.pnl,
                    "score": r.score
                }
            )

    return pd.DataFrame(rows)


# ============================================================
# FLAG PROFIT ANALYSIS
# ============================================================

def _compute_flag_stats(df: pd.DataFrame):

    stats = {}

    g = df.groupby("flag")

    for flag, sub in g:

        n = len(sub)

        if n < MIN_SAMPLE:
            continue

        win = (sub.pnl > 0).sum()

        win_rate = win / n

        avg_profit = sub.pnl.mean()

        expectancy = avg_profit * win_rate

        stats[flag] = {
            "trades": n,
            "win_rate": win_rate,
            "avg_profit": avg_profit,
            "expectancy": expectancy
        }

    return stats


# ============================================================
# CACHE UPDATE
# ============================================================

def update_profit_model(force=False):

    global _profit_stats_cache
    global _last_update

    now = dt.datetime.now()

    if not force:

        if _last_update and (now - _last_update).seconds < 600:
            return

    try:

        df = _load_trade_history()

        if df.empty:
            return

        df2 = _explode_flags(df)

        stats = _compute_flag_stats(df2)

        _profit_stats_cache = stats

        _last_update = now

        logger.info(
            "[PROFIT AI] updated flags=%s",
            len(stats)
        )

    except Exception:

        logger.exception("[PROFIT AI] update failed")


# ============================================================
# SCORE ADJUST
# ============================================================

def adjust_score_by_profit(score: float, flags: list):

    if not _profit_stats_cache:
        return score

    bonus = 0.0

    for f in flags:

        s = _profit_stats_cache.get(f)

        if not s:
            continue

        exp = s["expectancy"]

        bonus += exp * 0.2

    return score + bonus


# ============================================================
# RANKING BOOST
# ============================================================

def ranking_profit_boost(row):

    flags = []

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:
            flags.append(c)

    boost = 0

    for f in flags:

        s = _profit_stats_cache.get(f)

        if not s:
            continue

        boost += s["expectancy"]

    return boost


# ============================================================
# ENTRY FILTER
# ============================================================

def entry_profit_filter(row):

    flags = []

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:
            flags.append(c)

    bad = 0

    for f in flags:

        s = _profit_stats_cache.get(f)

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

def dump_profit_model():

    if not _profit_stats_cache:
        logger.info("[PROFIT AI] empty")
        return

    for k, v in sorted(
        _profit_stats_cache.items(),
        key=lambda x: -x[1]["expectancy"]
    )[:20]:

        logger.info(
            "[PROFIT AI] %s trades=%s win=%.2f exp=%.2f",
            k,
            v["trades"],
            v["win_rate"],
            v["expectancy"]
        )