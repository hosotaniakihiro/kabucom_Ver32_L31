# ============================================================
# File   : AI/night_ai_analyzer.py
# Ver    : 1.0.0-NIGHTAI-ANALYZER-BASE
# ------------------------------------------------------------
# ✔ ranking_entry_event × entry_log × exit_log 突合
# ✔ 勝率 / 平均pnl / 平均保有秒算出
# ✔ persistence / volume_speed / change_rate 別勝率算出
# ✔ 安全設計（例外吸収）
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from database.session import Session_position
from database.models import (
    RankingEntryEvent,
    EntryLog,
    ExitLog,
)

logger = logging.getLogger(__name__)


# ============================================================
# 1. データロード
# ============================================================

def _load_data():

    session = Session_position()

    try:
        events = pd.read_sql(
            session.query(RankingEntryEvent).statement,
            session.bind,
        )

        entries = pd.read_sql(
            session.query(EntryLog).statement,
            session.bind,
        )

        exits = pd.read_sql(
            session.query(ExitLog).statement,
            session.bind,
        )

    finally:
        session.close()

    return events, entries, exits


# ============================================================
# 2. event と trade の紐付け
# ============================================================

def _merge_trade(events, entries, exits):

    # symbol + 近い時間で紐付け（±120秒）
    entries["entry_time"] = pd.to_datetime(entries["entry_time"])
    events["event_time"] = pd.to_datetime(events["event_time"])

    merged = pd.merge_asof(
        entries.sort_values("entry_time"),
        events.sort_values("event_time"),
        left_on="entry_time",
        right_on="event_time",
        by="symbol",
        direction="backward",
        tolerance=pd.Timedelta(seconds=120),
    )

    # exit結合
    merged = merged.merge(
        exits[["trade_id", "pnl", "pnl_pct", "holding_seconds"]],
        on="trade_id",
        how="left",
    )

    return merged


# ============================================================
# 3. 勝率集計
# ============================================================

def _basic_stats(df):

    if df.empty:
        return {}

    total = len(df)
    wins = (df["pnl"] > 0).sum()

    return {
        "total_trades": total,
        "win_rate": round(wins / total, 4),
        "avg_pnl": round(df["pnl"].mean(), 2),
        "avg_hold_sec": round(df["holding_seconds"].mean(), 1),
    }


def _group_stats(df, col):

    if col not in df.columns:
        return {}

    result = {}

    for key, g in df.groupby(col):
        if len(g) < 5:
            continue

        win_rate = (g["pnl"] > 0).mean()
        result[key] = round(win_rate, 3)

    return result


# ============================================================
# 4. メイン解析
# ============================================================

def run_night_ai_analysis():

    logger.info("🌙 NightAI analysis started")

    events, entries, exits = _load_data()

    if events.empty or entries.empty:
        logger.warning("⚠ No data to analyze")
        return {}

    merged = _merge_trade(events, entries, exits)

    stats = {
        "basic": _basic_stats(merged),
        "by_persistence": _group_stats(merged, "rank_persistence"),
        "by_volume_speed": _group_stats(merged, "volume_speed"),
        "by_change_rate": _group_stats(merged, "change_rate"),
    }

    logger.info("🌙 NightAI analysis finished")

    return stats


# ============================================================
# 実行用
# ============================================================

if __name__ == "__main__":
    result = run_night_ai_analysis()
    print(result)