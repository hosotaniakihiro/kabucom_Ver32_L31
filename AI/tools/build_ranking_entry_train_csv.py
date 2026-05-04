# ============================================================
# AI/tools/build_ranking_entry_train_csv.py
# ------------------------------------------------------------
# ranking snapshot + 約定後リターン → 学習CSV生成
# future leak 完全防止
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# paths
# ------------------------------------------------------------
BASE_DIR = Path("AI")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_CSV = DATA_DIR / "ranking_entry_train.csv"

DB_RANKING = Path("database/ranking_snapshots.db")
DB_SUMMARY = Path("database/summary_1min.db")

# ------------------------------------------------------------
# label 設定
# ------------------------------------------------------------
PROFIT_TAKE_PCT = 0.3
LOSS_CUT_PCT = -0.2
HOLD_MINUTES = 3

# ------------------------------------------------------------
def build():
    logger.info("🚀 build ranking entry training csv")

    with sqlite3.connect(DB_RANKING) as con_rank, \
         sqlite3.connect(DB_SUMMARY) as con_sum:

        df_rank = pd.read_sql(
            "SELECT * FROM ranking_snapshot_1min",
            con_rank,
            parse_dates=["datetime"],
        )

        df_sum = pd.read_sql(
            "SELECT * FROM stock_summary_1min",
            con_sum,
            parse_dates=["datetime"],
        )

    # --------------------------------------------------------
    # merge（過去方向のみ）
    # --------------------------------------------------------
    df = pd.merge_asof(
        df_rank.sort_values("datetime"),
        df_sum.sort_values("datetime"),
        by="symbol",
        on="datetime",
        direction="backward",
    )

    # --------------------------------------------------------
    # future return 計算（ラベル専用）
    # --------------------------------------------------------
    df["future_close"] = (
        df.groupby("symbol")["close"]
        .shift(-HOLD_MINUTES)
    )

    df["future_return_pct"] = (
        (df["future_close"] - df["close"]) / df["close"] * 100
    )

    # --------------------------------------------------------
    # label
    # --------------------------------------------------------
    def label_func(r):
        if r >= PROFIT_TAKE_PCT:
            return "BUY"
        if r <= LOSS_CUT_PCT:
            return "SELL"
        return "NONE"

    df["entry_label"] = df["future_return_pct"].apply(label_func)

    # --------------------------------------------------------
    # feature selection（最低限）
    # --------------------------------------------------------
    feature_cols = [
        "symbol",
        "datetime",
        "rank",
        "rank_diff",
        "volume_speed",
        "ranking_score",
        "open",
        "high",
        "low",
        "close",
        "spread",
        "price_change_pct",
        "price_velocity",
        "vwap_distance",
        "atr",
        "atr_ratio",
        "volatility_1m",
        "index_return_1m",
        "index_volatility",
        "is_breakout",
        "is_pullback",
        "entry_label",
    ]

    df_out = df[feature_cols].dropna()

    df_out.to_csv(OUTPUT_CSV, index=False)
    logger.info(f"✅ saved -> {OUTPUT_CSV} rows={len(df_out)}")


if __name__ == "__main__":
    build()
