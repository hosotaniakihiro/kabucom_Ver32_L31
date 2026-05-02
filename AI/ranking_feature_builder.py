# ============================================================
# AI/ranking_feature_builder.py
# Ver25.0-FINAL-RANKING-FEATURE-BUILDER
# ------------------------------------------------------------
# ✔ ranking_snapshot_1min → AI用特徴量（1分1銘柄1行）
# ✔ 加工は「集約のみ」（RAW思想・再現性重視）
# ✔ 再実行安全（同一 snapshot_time は同一結果）
# ✔ 学習 / 推論 両対応
# ✔ DB直読み（pandas集約）
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from typing import List

import pandas as pd
from sqlalchemy import text

from database.session import ranking_engine

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _normalize_snapshot_time(snapshot_time) -> dt.datetime:
    """
    snapshot_time を datetime に統一し、分に正規化
    """
    if snapshot_time is None:
        snap = dt.datetime.now()
    elif isinstance(snapshot_time, str):
        snap = dt.datetime.fromisoformat(snapshot_time)
    elif isinstance(snapshot_time, dt.datetime):
        snap = snapshot_time
    else:
        raise ValueError(f"invalid snapshot_time: {snapshot_time}")

    return snap.replace(second=0, microsecond=0)


# ============================================================
# 特徴量定義
# ============================================================

FEATURE_COLUMNS: List[str] = [
    # 出現系
    "appear_count",        # 何ランキングに出たか
    "best_rank",           # 最良順位（小さいほど良い）
    "avg_rank",            # 平均順位

    # フラグ系
    "is_top10",            # TOP10 に1度でも出たか
    "is_gain_rank",        # 値上がり率系に出たか
    "is_volume_rank",      # 出来高 / 売買代金系に出たか
]


# ============================================================
# MAIN: ranking_feature_1min 構築
# ============================================================

def build_ranking_feature_1min(snapshot_time) -> pd.DataFrame:
    """
    ranking_snapshot_1min から
    AI用 ranking_feature_1min（1分1銘柄1行）を構築

    Returns
    -------
    pd.DataFrame
        columns:
          symbol
          snapshot_time
          + FEATURE_COLUMNS
    """

    snap = _normalize_snapshot_time(snapshot_time)

    # --------------------------------------------------------
    # DB 取得
    # --------------------------------------------------------
    sql = text("""
        SELECT
            symbol,
            snapshot_time,
            rank_type,
            rank_position
        FROM ranking_snapshot_1min
        WHERE snapshot_time = :snapshot_time
    """)

    try:
        with ranking_engine.connect() as conn:
            rows = conn.execute(
                sql, {"snapshot_time": snap}
            ).mappings().all()
    except Exception:
        logger.exception(
            "❌ ranking_snapshot_1min fetch failed @ %s",
            snap,
        )
        return pd.DataFrame()

    if not rows:
        logger.debug(
            "[ranking_feature] no snapshot rows @ %s",
            snap,
        )
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # --------------------------------------------------------
    # 集約（1分1銘柄）
    # --------------------------------------------------------
    feature_df = (
        df.groupby(["symbol", "snapshot_time"])
        .agg(
            appear_count=("rank_type", "count"),
            best_rank=("rank_position", "min"),
            avg_rank=("rank_position", "mean"),

            # フラグ系（存在判定）
            is_top10=(
                "rank_position",
                lambda x: int((x <= 10).any()),
            ),
            is_gain_rank=(
                "rank_type",
                lambda x: int(
                    any(
                        k in rt
                        for rt in x
                        for k in ("値上がり", "上昇")
                    )
                ),
            ),
            is_volume_rank=(
                "rank_type",
                lambda x: int(
                    any(
                        k in rt
                        for rt in x
                        for k in ("出来高", "売買高", "売買代金")
                    )
                ),
            ),
        )
        .reset_index()
    )

    # --------------------------------------------------------
    # 欠損防御（理論上は不要だが安全のため）
    # --------------------------------------------------------
    for col in FEATURE_COLUMNS:
        if col not in feature_df.columns:
            feature_df[col] = 0

    # 型を固定（AI事故防止）
    feature_df["appear_count"] = feature_df["appear_count"].astype(int)
    feature_df["best_rank"] = feature_df["best_rank"].astype(int)
    feature_df["avg_rank"] = feature_df["avg_rank"].astype(float)
    feature_df["is_top10"] = feature_df["is_top10"].astype(int)
    feature_df["is_gain_rank"] = feature_df["is_gain_rank"].astype(int)
    feature_df["is_volume_rank"] = feature_df["is_volume_rank"].astype(int)

    logger.info(
        "🧠 ranking_feature_1min built: %d symbols @ %s",
        len(feature_df),
        snap,
    )

    return feature_df


# ============================================================
# UTIL: 複数分まとめて構築（学習用）
# ============================================================

def build_ranking_feature_range(
    start_time,
    end_time,
) -> pd.DataFrame:
    """
    指定期間の snapshot_time をすべて特徴量化（学習用）
    """

    start = _normalize_snapshot_time(start_time)
    end = _normalize_snapshot_time(end_time)

    sql = text("""
        SELECT DISTINCT snapshot_time
        FROM ranking_snapshot_1min
        WHERE snapshot_time >= :start
          AND snapshot_time <= :end
        ORDER BY snapshot_time ASC
    """)

    try:
        with ranking_engine.connect() as conn:
            times = conn.execute(
                sql,
                {"start": start, "end": end},
            ).scalars().all()
    except Exception:
        logger.exception(
            "❌ snapshot_time range fetch failed: %s - %s",
            start, end,
        )
        return pd.DataFrame()

    frames = []

    for snap in times:
        df_feat = build_ranking_feature_1min(snap)
        if not df_feat.empty:
            frames.append(df_feat)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)