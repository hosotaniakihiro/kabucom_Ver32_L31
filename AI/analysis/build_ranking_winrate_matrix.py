# ============================================================
# File   : AI/analysis/build_ranking_winrate_matrix.py
# Ver    : 1.0.0-RANKING-WINRATE-MATRIX
# ------------------------------------------------------------
# ✔ ranking_entry_event × entry_log × exit_log 突合
# ✔ persistence × volume_speed 2D勝率算出
# ✔ ビン分割対応（volume_speed）
# ✔ 低サンプル除外
# ✔ 安全設計（例外吸収）
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from database.session import Session_position
from database.models import (
    RankingEntryEvent,
    EntryLog,
    ExitLog,
)

logger = logging.getLogger(__name__)

MIN_SAMPLE = 10  # 最低サンプル数


# ============================================================
# データロード
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
# 突合（±120秒以内）
# ============================================================

def _merge(events, entries, exits):

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

    merged = merged.merge(
        exits[["trade_id", "pnl"]],
        on="trade_id",
        how="left",
    )

    merged = merged.dropna(subset=["pnl"])

    return merged


# ============================================================
# volume_speed ビン分割
# ============================================================

def _bin_volume_speed(df, bins=(0, 1.2, 1.5, 2.0, 3.0, 10.0)):

    df["volume_speed_bin"] = pd.cut(
        df["volume_speed"],
        bins=bins,
        include_lowest=True,
    )

    return df


# ============================================================
# 2D勝率マトリクス生成
# ============================================================

def build_winrate_matrix():

    logger.info("📊 Building ranking winrate matrix...")

    events, entries, exits = _load_data()

    if events.empty or entries.empty:
        logger.warning("No data available.")
        return None

    merged = _merge(events, entries, exits)

    if merged.empty:
        logger.warning("No merged trades.")
        return None

    merged = _bin_volume_speed(merged)

    merged["win"] = merged["pnl"] > 0

    # 集計
    grouped = (
        merged
        .groupby(["rank_persistence", "volume_speed_bin"])
        .agg(
            trades=("win", "count"),
            win_rate=("win", "mean"),
        )
        .reset_index()
    )

    # サンプル数フィルタ
    grouped = grouped[grouped["trades"] >= MIN_SAMPLE]

    # ピボット
    matrix = grouped.pivot(
        index="rank_persistence",
        columns="volume_speed_bin",
        values="win_rate",
    )

    logger.info("📊 Matrix build complete.")

    return matrix


# ============================================================
# 実行用
# ============================================================

if __name__ == "__main__":

    matrix = build_winrate_matrix()

    if matrix is not None:
        print("\n=== Ranking Winrate Matrix ===\n")
        print(matrix.round(3))
    else:
        print("No matrix generated.")