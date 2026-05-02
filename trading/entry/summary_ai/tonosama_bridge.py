# ============================================================
# File   : trading/entry/summary_ai/tonosama_bridge.py
# Version: PRODUCTION-STABLE-REV1.0
# Purpose:
#   ranking_snapshot_1min 由来の殿様イナゴ候補を
#   summary AI entry runner に接続するブリッジ
#
# Flow:
#   ranking_snapshot_1min
#     ↓
#   ranking tonosama candidates
#     ↓
#   summary df と symbol 突合
#     ↓
#   AIgate へ渡す
# ============================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def filter_summary_by_ranking_tonosama(
    summary_df: pd.DataFrame,
    *,
    ranking_db_path: str | Path,
    max_candidates: int = 10,
) -> pd.DataFrame:
    """
    summary_df をランキング殿様イナゴ候補銘柄だけに絞る。

    注意:
      - この関数は発注しない
      - AIgate前の候補絞り込み専用
    """
    if summary_df is None or summary_df.empty:
        logger.info("[TONOSAMA BRIDGE] summary_df empty")
        return pd.DataFrame()

    try:
        from trading.ranking.tonosama.config import RankingTonosamaConfig
        from trading.ranking.tonosama.pipeline import build_ranking_tonosama_candidates_from_db
    except Exception:
        logger.exception("[TONOSAMA BRIDGE] import failed")
        return pd.DataFrame()

    config = RankingTonosamaConfig(
        max_candidates=max_candidates,
    )

    ranking_cand = build_ranking_tonosama_candidates_from_db(
        db_path=ranking_db_path,
        config=config,
    )

    if ranking_cand is None or ranking_cand.empty:
        logger.info("[TONOSAMA BRIDGE] no ranking tonosama candidates")
        return pd.DataFrame()

    if "symbol" not in ranking_cand.columns:
        logger.warning("[TONOSAMA BRIDGE] ranking candidates has no symbol")
        return pd.DataFrame()

    if "symbol" not in summary_df.columns:
        logger.warning("[TONOSAMA BRIDGE] summary_df has no symbol")
        return pd.DataFrame()

    symbols = set(ranking_cand["symbol"].astype(str).tolist())

    out = summary_df.copy()
    out["symbol"] = out["symbol"].astype(str)
    out = out[out["symbol"].isin(symbols)].copy()

    if out.empty:
        logger.info(
            "[TONOSAMA BRIDGE] no matched symbols ranking=%s summary_rows=%s",
            len(symbols),
            len(summary_df),
        )
        return pd.DataFrame()

    # ranking側の殿様スコアをsummary側へ付与
    merge_cols = [
        c for c in [
            "symbol",
            "ranking_tonosama_score",
            "price_change_1m_pct",
            "price_change_3m_pct",
            "volume_delta_1m",
            "volume_spike_ratio",
            "rank_up_speed",
            "first_appearance",
            "ranking_category_count",
        ]
        if c in ranking_cand.columns
    ]

    if len(merge_cols) > 1:
        out = out.merge(
            ranking_cand[merge_cols].drop_duplicates("symbol"),
            on="symbol",
            how="left",
        )

    sort_cols = [
        c for c in [
            "ranking_tonosama_score",
            "score_buy",
            "final_score",
            "display_score",
            "score",
        ]
        if c in out.columns
    ]

    if sort_cols:
        out = out.sort_values(sort_cols, ascending=[False] * len(sort_cols))

    out = out.drop_duplicates("symbol", keep="first").head(max_candidates).copy()

    logger.info(
        "[TONOSAMA BRIDGE] matched candidates=%s symbols=%s",
        len(out),
        ",".join(out["symbol"].astype(str).tolist()),
    )

    return out