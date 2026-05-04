# ============================================================
# trading/ranking/diff_updater.py
# ------------------------------------------------------------
# ランキング差分付与ユーティリティ
# ------------------------------------------------------------
# ✔ 前回ランキングとの差分を計算
# ✔ 新規ランクイン判定
# ✔ 初回・欠損データ完全耐性
# ✔ 判断・AI・スコアは一切行わない
# ============================================================

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
def ranking_diff_update(
    df_now: pd.DataFrame,
    df_prev: Optional[pd.DataFrame] = None,
    *,
    symbol_col: str = "symbol",
    rank_col: str = "rank",
) -> pd.DataFrame:
    """
    現在ランキングに差分情報を付与する

    Args:
        df_now (pd.DataFrame):
            今回のランキング
            必須列:
              - symbol_col
              - rank_col
        df_prev (pd.DataFrame | None):
            前回ランキング（None or 空なら全件新規扱い）
        symbol_col (str):
            銘柄コード列名
        rank_col (str):
            ランク列名（1 が最上位）

    Returns:
        pd.DataFrame:
            rank_diff / is_new を追加した DataFrame
    """

    # --------------------------------------------------------
    # 基本ガード
    # --------------------------------------------------------
    if df_now is None or df_now.empty:
        return df_now

    if symbol_col not in df_now.columns or rank_col not in df_now.columns:
        logger.warning(
            "ranking_diff_update: missing columns %s / %s",
            symbol_col, rank_col,
        )
        return df_now

    df = df_now.copy()

    # rank 正規化（数値化）
    try:
        df[rank_col] = pd.to_numeric(df[rank_col], errors="coerce")
    except Exception:
        pass

    # --------------------------------------------------------
    # 初回（前回なし）
    # --------------------------------------------------------
    if df_prev is None or df_prev.empty:
        df["rank_diff"] = 0
        df["is_new"] = True
        return df

    if symbol_col not in df_prev.columns or rank_col not in df_prev.columns:
        df["rank_diff"] = 0
        df["is_new"] = True
        return df

    # --------------------------------------------------------
    # 前回ランクマップ作成
    # --------------------------------------------------------
    try:
        prev_map = (
            df_prev[[symbol_col, rank_col]]
            .dropna()
            .assign(**{
                rank_col: lambda x: pd.to_numeric(x[rank_col], errors="coerce")
            })
            .dropna(subset=[rank_col])
            .set_index(symbol_col)[rank_col]
        )
    except Exception:
        logger.exception("failed to build prev_rank map")
        df["rank_diff"] = 0
        df["is_new"] = True
        return df

    # --------------------------------------------------------
    # 差分計算
    # --------------------------------------------------------
    def _calc_diff(row):
        prev_rank = prev_map.get(row[symbol_col])
        if prev_rank is None or pd.isna(prev_rank):
            return None
        if pd.isna(row[rank_col]):
            return None
        # 上昇：+ / 下降：-
        return prev_rank - row[rank_col]

    df["rank_diff"] = df.apply(_calc_diff, axis=1)
    df["is_new"] = df["rank_diff"].isna()

    return df
