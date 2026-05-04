# ============================================================
# File   : trading/ranking/rank_gap_engine.py
# Ver1.0-PRODUCTION-RANK-GAP-FINAL
# ------------------------------------------------------------
# ✔ LIVE × SNAPSHOT 統合
# ✔ rank_gap / score_gap 生成
# ✔ 相対順位ギャップ率生成
# ✔ スコア正規化対応
# ✔ NaN / inf 完全防御
# ✔ AI特徴量即利用可能
# ✔ 削除ゼロ思想
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_numeric(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    for col in df.columns:
        if df[col].dtype.kind in ("f", "i"):
            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], 0.0)
                .fillna(0.0)
            )

    return df


def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    if "symbol" in df.columns:
        df["symbol"] = (
            df["symbol"]
            .astype(str)
            .str.strip()
            .str.replace(".0", "", regex=False)
        )

    return df


# ============================================================
# MAIN: LIVE × SNAPSHOT MERGE
# ============================================================

def build_rank_gap(
    df_live: pd.DataFrame,
    df_snapshot: pd.DataFrame,
) -> pd.DataFrame:
    """
    LIVEランキングとSNAPSHOTランキングを統合し、
    GAP特徴量を生成する。

    Returns:
        DataFrame (LIVE基準 + GAP列付き)
    """

    try:

        if df_live is None or df_live.empty:
            logger.warning("[RANK_GAP] live empty")
            return pd.DataFrame()

        df_live = _normalize_symbol(df_live)
        df_snapshot = _normalize_symbol(df_snapshot)

        df = df_live.copy()

        # ----------------------------------------------------
        # SNAPSHOT側整形
        # ----------------------------------------------------
        if df_snapshot is None or df_snapshot.empty:
            logger.warning("[RANK_GAP] snapshot empty → gap=0")
            df["rank_gap"] = 0
            df["score_gap"] = 0.0
            df["rank_gap_ratio"] = 0.0
            df["score_gap_ratio"] = 0.0
            return _safe_numeric(df)

        snap = df_snapshot.copy()

        required_cols = ["symbol"]

        if "rank_snapshot" not in snap.columns:
            snap = (
                snap.sort_values("score_buy", ascending=False)
                .drop_duplicates(subset=["symbol"], keep="last")
                .reset_index(drop=True)
            )
            snap["rank_snapshot"] = np.arange(1, len(snap) + 1)

        if "score_buy" not in snap.columns:
            snap["score_buy"] = 0.0

        snap = snap[
            ["symbol", "rank_snapshot", "score_buy"]
        ].rename(
            columns={
                "score_buy": "score_buy_snapshot"
            }
        )

        # ----------------------------------------------------
        # MERGE
        # ----------------------------------------------------
        df = df.merge(
            snap,
            on="symbol",
            how="left",
        )

        # ----------------------------------------------------
        # GAP生成
        # ----------------------------------------------------
        if "rank_live" not in df.columns:
            df = (
                df.sort_values("score_buy", ascending=False)
                .reset_index(drop=True)
            )
            df["rank_live"] = np.arange(1, len(df) + 1)

        df["rank_gap"] = df["rank_live"] - df["rank_snapshot"]
        df["score_gap"] = df["score_buy"] - df["score_buy_snapshot"]

        # ----------------------------------------------------
        # 比率GAP生成（スケール依存防止）
        # ----------------------------------------------------
        max_rank = max(len(df), 1)

        df["rank_gap_ratio"] = df["rank_gap"] / max_rank

        df["score_gap_ratio"] = np.where(
            df["score_buy_snapshot"].abs() > 1e-9,
            df["score_gap"] / df["score_buy_snapshot"].abs(),
            0.0,
        )

        # ----------------------------------------------------
        # 欠損補正
        # ----------------------------------------------------
        df["rank_gap"] = df["rank_gap"].fillna(999)
        df["score_gap"] = df["score_gap"].fillna(0.0)
        df["rank_gap_ratio"] = df["rank_gap_ratio"].fillna(0.0)
        df["score_gap_ratio"] = df["score_gap_ratio"].fillna(0.0)

        df = _safe_numeric(df)

        logger.info(
            "[RANK_GAP] merged rows=%d",
            len(df),
        )

        return df

    except Exception:
        logger.exception("[RANK_GAP] fatal error")
        return pd.DataFrame()


# ============================================================
# フィルタ（実戦用）
# ============================================================

def filter_strong_consensus(
    df: pd.DataFrame,
    *,
    max_rank_gap: int = 20,
    min_score_gap_ratio: float = -0.5,
) -> pd.DataFrame:
    """
    LIVEとSNAPSHOTの乖離が小さい銘柄のみ抽出

    用途：
        ・両方強い銘柄抽出
        ・ノイズ除去
    """

    if df is None or df.empty:
        return pd.DataFrame()

    out = df[
        (df["rank_gap"].abs() <= max_rank_gap)
        &
        (df["score_gap_ratio"] >= min_score_gap_ratio)
    ].copy()

    return out


# ============================================================
# ノイズ検出（急騰専用）
# ============================================================

def filter_live_spike(
    df: pd.DataFrame,
    *,
    min_positive_gap_ratio: float = 1.0,
) -> pd.DataFrame:
    """
    LIVEだけ急騰している銘柄抽出

    用途：
        ・短期ブレイク検出
        ・PUSH依存銘柄
    """

    if df is None or df.empty:
        return pd.DataFrame()

    return df[
        df["score_gap_ratio"] >= min_positive_gap_ratio
    ].copy()