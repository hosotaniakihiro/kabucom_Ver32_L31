# ============================================================
# File   : trading/scoring/core/scoring_warmup_ranking.py
# Version: 1.1-FINAL-RANKING-NUMERIC-SAFE
# ------------------------------------------------------------
# ✔ 前日ランキング統合（SEED_ONLY用）
# ✔ ranking DB 未存在でも安全
# ✔ dict混入完全禁止
# ✔ 数値のみ加算
# ✔ symbol強制str化
# ✔ rank欠損完全防御
# ✔ 1min / 3min / 5min 全対応
# ✔ score_total が無くても自動生成
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np
import datetime as dt
import sqlite3
from pathlib import Path

from config.paths import get_path


# ============================================================
# 安全numeric変換
# ============================================================

def _to_numeric_safe(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype(float)


# ============================================================
# 前営業日取得
# ============================================================

def _get_previous_day() -> dt.date:
    return (dt.datetime.now() - dt.timedelta(days=1)).date()


# ============================================================
# ランキング読込
# ============================================================

def _load_previous_ranking(interval: int) -> pd.DataFrame:

    try:
        base_dir = get_path("ranking_db_dir")
    except Exception:
        return pd.DataFrame()

    prev = _get_previous_day()
    db_path = Path(base_dir) / f"ranking{prev.strftime('%Y%m%d')}.db"

    if not db_path.exists():
        return pd.DataFrame()

    table_name = f"ranking_snapshot_{interval}min"

    try:
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql(
                f"SELECT symbol, rank FROM {table_name}",
                conn
            )
    except Exception:
        return pd.DataFrame()

    if df.empty or "symbol" not in df.columns:
        return pd.DataFrame()

    df = df.copy()
    df["symbol"] = df["symbol"].astype(str)

    if "rank" in df.columns:
        df["rank"] = _to_numeric_safe(df["rank"])
    else:
        df["rank"] = np.nan

    df = df.dropna(subset=["rank"])
    df = df.sort_values("rank")

    return df


# ============================================================
# ranking warmup加算
# ============================================================

def scoring_warmup_ranking(
    df: pd.DataFrame,
    interval: int,
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # score_total 無い事故防止
    # --------------------------------------------------------
    if "score_total" not in df.columns:
        df["score_total"] = 0.0

    df["score_total"] = _to_numeric_safe(
        df["score_total"]
    ).fillna(0.0)

    ranking_df = _load_previous_ranking(interval)

    if ranking_df.empty:
        return df

    # --------------------------------------------------------
    # 上位層抽出
    # --------------------------------------------------------
    top10 = set(ranking_df.head(10)["symbol"])
    top20 = set(ranking_df.head(20)["symbol"])
    top50 = set(ranking_df.head(50)["symbol"])

    # --------------------------------------------------------
    # ボーナス計算
    # --------------------------------------------------------
    bonus = np.zeros(len(df), dtype=float)

    symbols = df["symbol"].astype(str).values

    for i, sym in enumerate(symbols):

        if sym in top10:
            bonus[i] = 3.0
        elif sym in top20:
            bonus[i] = 2.0
        elif sym in top50:
            bonus[i] = 1.0
        else:
            bonus[i] = 0.0

    df["score_total"] += bonus

    # --------------------------------------------------------
    # BUY / SELL 再生成（数値のみ）
    # --------------------------------------------------------
    df["score_buy"] = np.where(
        df["score_total"] > 0,
        df["score_total"],
        0.0
    ).astype(float)

    df["score_sell"] = np.where(
        df["score_total"] < 0,
        -df["score_total"],
        0.0
    ).astype(float)

    df["score_reasons"] = "warmup_ranking"

    return df