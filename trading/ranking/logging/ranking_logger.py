# ============================================================
# File   : trading/ranking/logging/ranking_logger.py
# Version: Ver3.1-PRODUCTION-ULTRA-RANKING-LOGGER-ELITE-FIXED
# ------------------------------------------------------------
# ✔ 既存機能100%保持（削除ゼロ）
# ✔ 表示ズレ修正（最重要）
# ✔ 順位番号修正
# ✔ debug可視化強化
# ✔ CSV軽量化
# ✔ NaN / inf safe
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import os
import datetime as dt
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TOP_N = 10
SAVE_CSV = True
SAVE_DB = False

LOG_DIR = "logs/ranking"


# ============================================================
# helpers
# ============================================================

def _safe_df(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    return df.copy()


def _sanitize(df: pd.DataFrame) -> pd.DataFrame:

    try:
        num_cols = df.select_dtypes(include=np.number).columns

        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )
    except Exception:
        logger.exception("[ranking_logger] sanitize failed")

    return df


def _ensure_dir():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass


def _now_str():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# ============================================================
# safe getter（NEW）
# ============================================================

def _g(row, col, default=0.0):
    v = row.get(col, default)
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return default
        return float(v)
    except Exception:
        return default


# ============================================================
# console pretty print（修正版）
# ============================================================

def _print_top(df: pd.DataFrame, interval: int):

    if df.empty:
        logger.info("[RANKING] empty")
        return

    try:

        df_top = df.head(TOP_N).reset_index(drop=True)

        print("\n========== 📊 RANKING TOP{} ({}min) ==========".format(TOP_N, interval))

        for rank, row in df_top.iterrows():

            print(
                "{:>2}. {:<6} score={:>7.3f} base={:>5.2f} trend={:>5.2f} mom={:>5.2f}".format(
                    rank + 1,
                    str(row.get("symbol", "")),
                    _g(row, "score"),
                    _g(row, "_score_base"),
                    _g(row, "_score_trend"),
                    _g(row, "_score_momentum"),
                )
            )

    except Exception:
        logger.exception("[ranking_logger] print failed")


# ============================================================
# CSV保存（軽量化）
# ============================================================

def _save_csv(df: pd.DataFrame, interval: int):

    if not SAVE_CSV:
        return

    try:

        _ensure_dir()

        filename = f"{LOG_DIR}/ranking_{interval}m_{_now_str()}.csv"

        # 🔥 軽量化（重要）
        cols = [
            "symbol",
            "score",
            "_score_base",
            "_score_trend",
            "_score_momentum",
            "_score_velocity",
        ]

        export_cols = [c for c in cols if c in df.columns]

        df[export_cols].to_csv(filename, index=False)

        logger.info("[ranking_logger] saved csv: %s", filename)

    except Exception:
        logger.exception("[ranking_logger] csv save failed")


# ============================================================
# SQLite保存（そのまま）
# ============================================================

def _save_db(df: pd.DataFrame, interval: int):

    if not SAVE_DB:
        return

    try:

        import sqlite3

        _ensure_dir()

        db_path = f"{LOG_DIR}/ranking.db"

        conn = sqlite3.connect(db_path)

        df.to_sql(
            f"ranking_{interval}m",
            conn,
            if_exists="append",
            index=False
        )

        conn.close()

    except Exception:
        logger.exception("[ranking_logger] db save failed")


# ============================================================
# stats（強化）
# ============================================================

def _log_stats(df: pd.DataFrame):

    try:

        logger.info(
            "[RANKING STATS] count=%s mean=%.4f max=%.4f min=%.4f std=%.4f",
            len(df),
            df["score"].mean(),
            df["score"].max(),
            df["score"].min(),
            df["score"].std(),
        )

    except Exception:
        pass


# ============================================================
# debug distribution（NEW）
# ============================================================

def _log_distribution(df: pd.DataFrame):

    try:

        if "_score_base" in df.columns:

            logger.debug(
                "[DIST] base mean=%.3f trend=%.3f mom=%.3f vel=%.3f",
                df["_score_base"].mean(),
                df["_score_trend"].mean(),
                df["_score_momentum"].mean(),
                df.get("_score_velocity", pd.Series(0)).mean(),
            )

    except Exception:
        pass


# ============================================================
# main
# ============================================================

def log_ranking(
    df: pd.DataFrame,
    *,
    interval: int
) -> None:

    df = _safe_df(df)

    if df.empty:
        logger.info("[ranking_logger] empty df")
        return

    try:

        df = _sanitize(df)

        # sort
        if "score" in df.columns:
            df = df.sort_values("score", ascending=False)

        # console
        _print_top(df, interval)

        # stats
        _log_stats(df)

        # debug（NEW）
        _log_distribution(df)

        # save
        _save_csv(df, interval)
        _save_db(df, interval)

    except Exception:
        logger.exception("[ranking_logger] failed")