# ============================================================
# File   : AI/train/train_ranking_auto.py
# Ver    : 1.1-FINAL-RANKING-AUTO-TRAIN-CLEAN
# ------------------------------------------------------------
# ✔ ranking_raw_1min × trade_exit_stats から自動学習
# ✔ 勝ち / 負け（is_win）二値分類
# ✔ LightGBM 使用
# ✔ データ不足時は安全にスキップ
# ✔ scheduler / タスク実行前提
# ✔ 本番トレードロジック完全非侵入
# ============================================================

import sqlite3
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd
import lightgbm as lgb

# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# PATH 設定
# ============================================================

MODEL_DIR = Path("AI/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODEL_DIR / "ranking_entry_lgbm.txt"

# ranking DB（環境に合わせて変更可）
DB_PATH = Path("X:/raw_data/kabu_station/ranking/ranking_latest.db")

# ============================================================
# SQL（RAW × 実トレード結果）
# ============================================================

TRAIN_SQL = """
SELECT
    r.symbol,
    r.rank_type_id,
    r.rank_position,
    r.market,
    r.value,
    r.trading_volume,
    r.trading_value,
    r.tick_count,

    t.side,
    t.entry_price,
    t.exit_price,
    t.mfe_pct,
    t.mae_pct,

    CASE
        WHEN t.exit_price > t.entry_price THEN 1
        ELSE 0
    END AS is_win

FROM ranking_raw_1min r
JOIN trade_exit_stats t
  ON r.symbol = t.symbol
 AND ABS(
     strftime('%s', r.snapshot_time) -
     strftime('%s', t.created_at)
 ) <= 300
WHERE t.is_valid = 1
"""

# ============================================================
# 特徴量定義
# ============================================================

FEATURE_COLS = [
    "rank_type_id",
    "rank_position",
    "value",
    "trading_volume",
    "trading_value",
    "tick_count",
]

TARGET_COL = "is_win"

MIN_TRAIN_ROWS = 300

# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("🤖 [RANKING AUTO TRAIN] START")

    if not DB_PATH.exists():
        logger.error("❌ DB not found: %s", DB_PATH)
        return

    # --------------------------------------------------------
    # LOAD
    # --------------------------------------------------------
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(TRAIN_SQL, conn)

    if df.empty:
        logger.warning("⚠ training data empty")
        return

    # --------------------------------------------------------
    # CLEAN
    # --------------------------------------------------------
    df = df.dropna(subset=FEATURE_COLS + [TARGET_COL])

    if len(df) < MIN_TRAIN_ROWS:
        logger.warning(
            "⚠ not enough rows: %d < %d",
            len(df),
            MIN_TRAIN_ROWS,
        )
        return

    X = df[FEATURE_COLS]
    y = df[TARGET_COL]

    # --------------------------------------------------------
    # DATASET
    # --------------------------------------------------------
    train_ds = lgb.Dataset(X, label=y)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbosity": -1,
        "seed": 42,
    }

    logger.info(
        "🧠 training rows=%d features=%d",
        len(X),
        len(FEATURE_COLS),
    )

    # --------------------------------------------------------
    # TRAIN
    # --------------------------------------------------------
    model = lgb.train(
        params,
        train_ds,
        num_boost_round=200,
    )

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------
    model.save_model(str(MODEL_PATH))

    logger.info("✅ model saved: %s", MODEL_PATH)
    logger.info("🎉 [RANKING AUTO TRAIN] DONE")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
