# ============================================================
# trading/ranking/ranking_aggregate_builder.py
# Ver1.2-FINAL-RANKING-AGGREGATE-BREADTH-STRENGTH-STABLE
# ------------------------------------------------------------
# ✔ 複数ランキング種別を symbol 単位で統合
# ✔ 出現回数 / 最良順位 / 平均順位 を完全保持
# ✔ breadth（話題性の広さ）を固定スケールで安定評価
# ✔ strength（順位の強さ）を best + avg の複合で評価
# ✔ ranking_summary_adapter への入力ユニバース生成専用
# ✔ SUMMARY / ENTRY / AI ロジックとは完全分離
# ✔ universe 依存・瞬間ブレを完全排除
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from typing import Dict

logger = logging.getLogger(__name__)

# ============================================================
# 設定（魔法数排除・思想固定）
# ============================================================

DEFAULT_MAX_RANK = 50          # ランキング上位何位までを見るか

WEIGHT_BREADTH = 0.4           # 出現ランキング種別の広さ
WEIGHT_STRENGTH = 0.6          # 順位の強さ（持続性重視）

# ============================================================
# メイン API
# ============================================================

def build_ranking_aggregate(
    ranking_df: pd.DataFrame,
    *,
    max_rank: int = DEFAULT_MAX_RANK,
) -> pd.DataFrame:
    """
    複数ランキングを symbol 単位で統合する

    必須列:
        symbol
        rank_type
        rank_position

    Returns:
        DataFrame:
            symbol
            rank_types_count
            best_rank
            avg_rank
            breadth_score
            strength_score
            ranking_score_total
    """

    # --------------------------------------------------------
    # ガード（入力）
    # --------------------------------------------------------
    if ranking_df is None or ranking_df.empty:
        logger.warning("[RANKING_AGG] input empty")
        return pd.DataFrame()

    required_cols = {"symbol", "rank_type", "rank_position"}
    missing = required_cols - set(ranking_df.columns)
    if missing:
        logger.error(
            "[RANKING_AGG] missing required columns: %s",
            ",".join(sorted(missing)),
        )
        return pd.DataFrame()

    df = ranking_df.copy()

    # --------------------------------------------------------
    # 正規化
    # --------------------------------------------------------
    df["symbol"] = df["symbol"].astype(str)
    df["rank_type"] = df["rank_type"].astype(str)

    df["rank_position"] = pd.to_numeric(
        df["rank_position"],
        errors="coerce",
    )

    # NaN / inf 排除
    df = df.dropna(subset=["symbol", "rank_type", "rank_position"])

    # 上位 rank のみ使用
    df = df[df["rank_position"] <= max_rank]

    if df.empty:
        logger.warning("[RANKING_AGG] no rows after rank filter")
        return pd.DataFrame()

    # --------------------------------------------------------
    # ranking universe 情報
    # --------------------------------------------------------
    total_rank_types = df["rank_type"].nunique()
    if total_rank_types <= 0:
        logger.warning("[RANKING_AGG] no rank_type detected")
        return pd.DataFrame()

    # --------------------------------------------------------
    # 集計（symbol 単位）
    # --------------------------------------------------------
    agg = (
        df.groupby("symbol", as_index=False)
        .agg(
            rank_types_count=("rank_type", "nunique"),
            best_rank=("rank_position", "min"),
            avg_rank=("rank_position", "mean"),
        )
    )

    if agg.empty:
        logger.warning("[RANKING_AGG] aggregation result empty")
        return pd.DataFrame()

    # --------------------------------------------------------
    # スコア化
    # --------------------------------------------------------

    # ========================================================
    # breadth: 何種類のランキングに出たか
    # ・universe 最大値依存を完全排除
    # ・日跨ぎ / 時間跨ぎで比較可能
    # ========================================================
    agg["breadth_score"] = (
        agg["rank_types_count"] / float(total_rank_types)
    ).clip(0.0, 1.0)

    # ========================================================
    # strength: 順位の強さ（持続性重視）
    # ・best_rank = 瞬間最大強度
    # ・avg_rank  = 継続的な強度
    # ========================================================
    best_norm = 1.0 - (agg["best_rank"] / float(max_rank))
    avg_norm = 1.0 - (agg["avg_rank"] / float(max_rank))

    agg["strength_score"] = (
        0.6 * best_norm
        + 0.4 * avg_norm
    ).clip(0.0, 1.0)

    # ========================================================
    # total score（思想固定）
    # ========================================================
    agg["ranking_score_total"] = (
        WEIGHT_BREADTH * agg["breadth_score"]
        + WEIGHT_STRENGTH * agg["strength_score"]
    )

    # --------------------------------------------------------
    # 並び替え
    # --------------------------------------------------------
    agg = (
        agg
        .sort_values("ranking_score_total", ascending=False)
        .reset_index(drop=True)
    )

    logger.info(
        "[RANKING_AGG] aggregated symbols=%d (rank_types=%d)",
        len(agg),
        total_rank_types,
    )

    return agg