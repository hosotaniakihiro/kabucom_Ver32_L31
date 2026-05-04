# ============================================================
# File   : trading/ranking/live_ranking_engine.py
# Ver1.0-PRODUCTION-LIVE-RANKING-FINAL
# ------------------------------------------------------------
# ✔ HYBRID 1min 前提
# ✔ indicator 計算完全対応
# ✔ scoring_main 完全互換
# ✔ LIVE順位生成
# ✔ snapshot順位 merge対応
# ✔ GAP列生成
# ✔ 無限 / NaN 完全防御
# ✔ ranking_trigger 互換
# ✔ 削除ゼロ思想
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.scoring.core.scoring_core import scoring_main

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """
    無限値 / NaN / 型崩れ完全防御
    """
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
# LIVE RANKING BUILD
# ============================================================

def build_live_ranking(
    summary_1min: pd.DataFrame,
    snapshot_ranking_df: pd.DataFrame | None = None,
    *,
    interval: int = 1,
) -> pd.DataFrame:
    """
    LIVEランキング生成エンジン

    Parameters
    ----------
    summary_1min : HYBRID 1min dataframe
    snapshot_ranking_df : snapshotランキング（任意）
    interval : 1固定だが将来拡張用

    Returns
    -------
    DataFrame (LIVE ranking with optional GAP columns)
    """

    try:

        if summary_1min is None or summary_1min.empty:
            logger.warning("[LIVE_RANK] summary empty")
            return pd.DataFrame()

        df = summary_1min.copy()

        df = _normalize_symbol(df)

        # ----------------------------------------------------
        # Indicator 計算
        # ----------------------------------------------------
        df = add_all_indicators(df, interval=interval)

        # ----------------------------------------------------
        # Scoring
        # ----------------------------------------------------
        df = scoring_main(df, interval=interval)

        df = _safe_numeric(df)

        # ----------------------------------------------------
        # LIVE順位生成
        # ----------------------------------------------------
        if "score_buy" not in df.columns:
            logger.warning("[LIVE_RANK] score_buy missing")
            return df

        df = (
            df.sort_values("score_buy", ascending=False)
            .drop_duplicates(subset=["symbol"], keep="last")
            .reset_index(drop=True)
        )

        df["rank_live"] = np.arange(1, len(df) + 1)

        # ----------------------------------------------------
        # SNAPSHOT統合（任意）
        # ----------------------------------------------------
        if (
            snapshot_ranking_df is not None
            and not snapshot_ranking_df.empty
        ):

            snap = snapshot_ranking_df.copy()

            snap = _normalize_symbol(snap)

            if "rank_snapshot" not in snap.columns:
                snap = (
                    snap.sort_values("score_buy", ascending=False)
                    .drop_duplicates(subset=["symbol"], keep="last")
                    .reset_index(drop=True)
                )
                snap["rank_snapshot"] = np.arange(1, len(snap) + 1)

            snap_cols = [
                c for c in [
                    "symbol",
                    "rank_snapshot",
                    "score_buy"
                ] if c in snap.columns
            ]

            snap = snap[snap_cols].rename(
                columns={
                    "score_buy": "score_buy_snapshot"
                }
            )

            df = df.merge(
                snap,
                on="symbol",
                how="left",
            )

            # GAP計算
            if "rank_snapshot" in df.columns:
                df["rank_gap"] = (
                    df["rank_live"] - df["rank_snapshot"]
                )
            else:
                df["rank_gap"] = 999

            if "score_buy_snapshot" in df.columns:
                df["score_gap"] = (
                    df["score_buy"] - df["score_buy_snapshot"]
                )
            else:
                df["score_gap"] = 0.0

            df["rank_gap"] = df["rank_gap"].fillna(999)
            df["score_gap"] = df["score_gap"].fillna(0.0)

        else:
            df["rank_gap"] = 0
            df["score_gap"] = 0.0

        # ----------------------------------------------------
        # 最終整形
        # ----------------------------------------------------
        df = _safe_numeric(df)

        logger.info(
            "[LIVE_RANK] built rows=%d top=%s",
            len(df),
            df.iloc[0]["symbol"] if len(df) > 0 else "-",
        )

        return df

    except Exception:
        logger.exception("[LIVE_RANK] fatal error")
        return pd.DataFrame()


# ============================================================
# TOP抽出ユーティリティ
# ============================================================

def get_live_top_n(
    df_live: pd.DataFrame,
    n: int = 10,
) -> pd.DataFrame:

    if df_live is None or df_live.empty:
        return pd.DataFrame()

    return df_live.sort_values("rank_live").head(n).copy()