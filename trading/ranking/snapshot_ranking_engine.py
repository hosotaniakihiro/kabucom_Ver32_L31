# ============================================================
# File   : trading/ranking/snapshot_ranking_engine.py
# Ver1.1-PRODUCTION-SNAPSHOT-RANKING-FINAL-ETF-FILTER
# ------------------------------------------------------------
# ✔ Ver1.0 完全互換（削除ゼロ）
# ✔ ranking_snapshot_1min 前提
# ✔ indicator 再計算対応
# ✔ scoring_main 完全互換
# ✔ snapshot順位生成
# ✔ LIVE連携（rank_gap対応）
# ✔ 無限 / NaN 完全防御
# ✔ 将来interval拡張対応
# ✔ ETF / PRO Market 自動除外（NEW）
# ✔ symbol_flags DB 参照（NEW）
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np
import sqlite3
from pathlib import Path

from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.scoring.core.scoring_core import scoring_main

logger = logging.getLogger(__name__)


# ============================================================
# SYMBOL FLAGS DB
# ============================================================

SYMBOL_FLAGS_DB = r"X:\Basic\symbol_flags.db"


def _load_symbol_flags():

    try:

        if not Path(SYMBOL_FLAGS_DB).exists():
            logger.warning("[SNAP_RANK] symbol_flags DB not found")
            return pd.DataFrame()

        conn = sqlite3.connect(SYMBOL_FLAGS_DB)

        df = pd.read_sql(
            """
            SELECT
                symbol,
                is_etf,
                market_type
            FROM symbol_flags
            """,
            conn,
        )

        conn.close()

        df["symbol"] = df["symbol"].astype(str)

        return df

    except Exception:
        logger.exception("[SNAP_RANK] load symbol_flags failed")
        return pd.DataFrame()


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
# ETF / PRO Market 除外
# ============================================================

def _remove_non_stock(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    flags = _load_symbol_flags()

    if flags is None or flags.empty:
        return df

    df = df.merge(flags, on="symbol", how="left")

    df = df[
        (df["is_etf"].fillna(0) != 1)
        & (df["market_type"].fillna("") != "PRO Market")
    ]

    df = df.drop(columns=["is_etf", "market_type"], errors="ignore")

    return df


# ============================================================
# SNAPSHOT RANKING BUILD
# ============================================================

def build_snapshot_ranking(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
    recalc_indicator: bool = True,
    recalc_scoring: bool = True,
) -> pd.DataFrame:
    """
    SNAPSHOTランキング生成
    """

    try:

        if snapshot_df is None or snapshot_df.empty:
            logger.warning("[SNAP_RANK] snapshot empty")
            return pd.DataFrame()

        df = snapshot_df.copy()

        df = _normalize_symbol(df)

        # ----------------------------------------------------
        # ETF / PRO Market 除外
        # ----------------------------------------------------
        df = _remove_non_stock(df)

        # ----------------------------------------------------
        # Indicator再計算
        # ----------------------------------------------------
        if recalc_indicator:
            df = add_all_indicators(df, interval=interval)

        # ----------------------------------------------------
        # Scoring再計算
        # ----------------------------------------------------
        if recalc_scoring:
            df = scoring_main(df, interval=interval)

        df = _safe_numeric(df)

        if "score_buy" not in df.columns:
            logger.warning("[SNAP_RANK] score_buy missing")
            return df

        # ----------------------------------------------------
        # SNAPSHOT順位生成
        # ----------------------------------------------------
        df = (
            df.sort_values("score_buy", ascending=False)
            .drop_duplicates(subset=["symbol"], keep="last")
            .reset_index(drop=True)
        )

        df["rank_snapshot"] = np.arange(1, len(df) + 1)

        logger.info(
            "[SNAP_RANK] built rows=%d top=%s",
            len(df),
            df.iloc[0]["symbol"] if len(df) > 0 else "-",
        )

        return df

    except Exception:
        logger.exception("[SNAP_RANK] fatal error")
        return pd.DataFrame()


# ============================================================
# GAP統合（LIVEとの結合用）
# ============================================================

def merge_with_live_ranking(
    df_snapshot: pd.DataFrame,
    df_live: pd.DataFrame,
) -> pd.DataFrame:

    if (
        df_snapshot is None
        or df_snapshot.empty
        or df_live is None
        or df_live.empty
    ):
        return df_snapshot

    df_snapshot = df_snapshot.copy()
    df_live = df_live.copy()

    df_snapshot = _normalize_symbol(df_snapshot)
    df_live = _normalize_symbol(df_live)

    live_cols = [
        c
        for c in [
            "symbol",
            "rank_live",
            "score_buy",
        ]
        if c in df_live.columns
    ]

    live_df = df_live[live_cols].rename(
        columns={
            "score_buy": "score_buy_live"
        }
    )

    df = df_snapshot.merge(
        live_df,
        on="symbol",
        how="left",
    )

    # GAP生成
    if "rank_live" in df.columns:
        df["rank_gap"] = df["rank_live"] - df["rank_snapshot"]
    else:
        df["rank_gap"] = 999

    if "score_buy_live" in df.columns:
        df["score_gap"] = df["score_buy_live"] - df["score_buy"]
    else:
        df["score_gap"] = 0.0

    df["rank_gap"] = df["rank_gap"].fillna(999)
    df["score_gap"] = df["score_gap"].fillna(0.0)

    df = _safe_numeric(df)

    return df


# ============================================================
# TOP抽出ユーティリティ
# ============================================================

def get_snapshot_top_n(
    df_snapshot: pd.DataFrame,
    n: int = 10,
) -> pd.DataFrame:

    if df_snapshot is None or df_snapshot.empty:
        return pd.DataFrame()

    return df_snapshot.sort_values("rank_snapshot").head(n).copy()