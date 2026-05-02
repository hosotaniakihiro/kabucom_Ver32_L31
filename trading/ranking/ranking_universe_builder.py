# ============================================================
# trading/ranking/ranking_universe_builder.py
# Ver1.0-FINAL-RANKING-UNIVERSE-BUILDER
# ------------------------------------------------------------
# ✔ ranking_snapshot / ranking_raw から評価対象銘柄を決定
# ✔ config 駆動（魔法数なし）
# ✔ 最大銘柄数を制限
# ✔ price / turnover / rank_position ガード
# ============================================================

from __future__ import annotations
import pandas as pd
from typing import List

from config.ranking_entry_config import RANKING_ENTRY_CONFIG


def build_ranking_universe(
    ranking_df: pd.DataFrame,
    *,
    max_symbols: int = 80,
) -> List[str]:
    """
    ranking DF から評価対象銘柄ユニバースを構築する
    """

    if ranking_df is None or ranking_df.empty:
        return []

    cfg_rank = RANKING_ENTRY_CONFIG["RANKING"]
    cfg_vol = RANKING_ENTRY_CONFIG["VOLUME"]
    cfg_price = RANKING_ENTRY_CONFIG["PRICE"]

    df = ranking_df.copy()

    # ------------------------------
    # rank_position ガード
    # ------------------------------
    if "rank_position" in df.columns and cfg_rank.get("MAX_RANK_POSITION"):
        df = df[df["rank_position"] <= cfg_rank["MAX_RANK_POSITION"]]

    # ------------------------------
    # price ガード
    # ------------------------------
    price_col = "current_price" if "current_price" in df.columns else "price"
    if price_col in df.columns:
        df = df[
            (df[price_col] >= cfg_price["MIN"])
            & (df[price_col] <= cfg_price["MAX"])
        ]

    # ------------------------------
    # turnover ガード
    # ------------------------------
    if "turnover" in df.columns:
        df = df[df["turnover"] >= cfg_vol["MIN_TURNOVER"]]

    # ------------------------------
    # symbol 抽出（重複排除）
    # ------------------------------
    symbols = (
        df["symbol"]
        .astype(str)
        .dropna()
        .drop_duplicates()
        .tolist()
    )

    return symbols[:max_symbols]