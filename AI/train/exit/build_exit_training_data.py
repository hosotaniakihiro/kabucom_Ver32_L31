# ============================================================
# AI/train/exit/build_exit_training_data.py
# Ver1.1.0-FINAL-EXIT-TRAINING-DATA-BUILDER
# ------------------------------------------------------------
# ✔ trade_exit_stats から EXIT AI 用教師データ生成
# ✔ EXIT 良否ラベル付与（analysis と完全一致）
# ✔ 数値特徴量のみ抽出（LightGBM 前提）
# ✔ CSV / Parquet 出力対応
# ✔ DB 書き込みなし（read-only）
# ✔ 欠損・0除算・非数値に完全耐性
# ============================================================

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config.paths import get_path


# ============================================================
# 設定
# ============================================================

DB_PATH: Path = get_path("position_db")     # positions.db
TABLE = "trade_exit_stats"

OUT_DIR: Path = get_path("ai_train_exit")
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_CSV = OUT_DIR / "exit_training_data.csv"
OUT_PARQUET = OUT_DIR / "exit_training_data.parquet"

# ------------------------------------------------------------
# ラベル判定ルール（analysis と完全一致）
# ------------------------------------------------------------

GOOD_EXIT_MIN_PNL_PCT = 0.15     # +0.15%以上は良い EXIT
MISS_THRESHOLD_PCT    = 0.30     # 30%以上の取り逃しは悪い
SHORT_HOLD_SEC        = 10       # 超短期成功は良い


# ============================================================
# ロード
# ============================================================

def load_exit_stats() -> pd.DataFrame:
    """
    trade_exit_stats を read-only でロード
    """
    sql = f"""
        SELECT
            trade_id,
            symbol,
            side,
            entry_price,
            exit_price,
            atr_1min,
            mfe,
            mae,
            mfe_pct,
            mae_pct,
            holding_seconds,
            exit_reason,
            index_shock,
            is_valid
        FROM {TABLE}
    """

    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(sql, conn)

    if df.empty:
        raise RuntimeError("❌ trade_exit_stats is empty")

    return df


# ============================================================
# 特徴量生成
# ============================================================

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    数値特徴量生成（安全）
    """
    df = df.copy()

    # ------------------------------
    # 実現損益率（%）
    # ------------------------------
    df["pnl_pct"] = (
        (df["exit_price"] - df["entry_price"]) / df["entry_price"] * 100
    )
    df.loc[df["side"] == "SELL", "pnl_pct"] *= -1

    # ------------------------------
    # 取り逃し（%）
    # ------------------------------
    df["miss_pct"] = df["mfe_pct"] - df["pnl_pct"]

    # ------------------------------
    # ATR 正規化（0除算防止）
    # ------------------------------
    df["mfe_atr"] = np.where(
        df["atr_1min"] > 0,
        df["mfe"] / df["atr_1min"],
        0.0,
    )

    df["mae_atr"] = np.where(
        df["atr_1min"] > 0,
        df["mae"] / df["atr_1min"],
        0.0,
    )

    # ------------------------------
    # HOLD 時間（log 正規化）
    # ------------------------------
    df["holding_seconds"] = df["holding_seconds"].fillna(0)
    df["hold_sec_log"] = np.log(df["holding_seconds"] + 1.0)

    return df


# ============================================================
# EXIT ラベル生成
# ============================================================

def classify_exit(row: pd.Series) -> int:
    """
    EXIT AI 用ラベル

      1 : 良い EXIT
      0 : 普通
     -1 : 悪い EXIT
    """

    # 学習無効データは中立
    if not bool(row.get("is_valid", True)):
        return 0

    pnl_pct = float(row.get("pnl_pct", 0.0))
    miss_pct = float(row.get("miss_pct", 0.0))
    hold_sec = int(row.get("holding_seconds", 0))
    exit_reason = row.get("exit_reason")

    # 強制ロス
    if exit_reason in ("HARD_STOP",):
        return -1

    # 十分な利益
    if pnl_pct >= GOOD_EXIT_MIN_PNL_PCT:
        return 1

    # 大きな取り逃し
    if miss_pct >= MISS_THRESHOLD_PCT:
        return -1

    # 超短期成功
    if hold_sec <= SHORT_HOLD_SEC and pnl_pct > 0:
        return 1

    return 0


# ============================================================
# 学習用データ構築
# ============================================================

def build_training_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    学習用 DataFrame 構築
    """
    df = df.copy()

    # ラベル付与
    df["label"] = df.apply(classify_exit, axis=1)

    # ------------------------------
    # 学習用特徴量（数値のみ）
    # ------------------------------
    feature_cols = [
        # 価格・リスク
        "atr_1min",
        "mfe_pct",
        "mae_pct",
        "mfe_atr",
        "mae_atr",

        # 時間
        "holding_seconds",
        "hold_sec_log",

        # 成果
        "pnl_pct",
        "miss_pct",

        # 市場環境
        "index_shock",
    ]

    cols = feature_cols + ["label"]

    train_df = (
        df[cols]
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
        .reset_index(drop=True)
    )

    if train_df.empty:
        raise RuntimeError("❌ training data is empty after cleaning")

    return train_df


# ============================================================
# 保存
# ============================================================

def save_outputs(df: pd.DataFrame) -> None:
    """
    CSV / Parquet 保存
    """
    df.to_csv(OUT_CSV, index=False)
    df.to_parquet(OUT_PARQUET, index=False)

    print("✅ EXIT training data saved")
    print(f"  CSV     : {OUT_CSV}")
    print(f"  Parquet : {OUT_PARQUET}")
    print(f"  Rows    : {len(df)}")


# ============================================================
# メイン
# ============================================================

def main():
    print("📥 loading trade_exit_stats...")
    raw = load_exit_stats()

    print("🧠 building features...")
    feat = build_features(raw)

    print("🏷 labeling exits...")
    train_df = build_training_df(feat)

    print("💾 saving outputs...")
    save_outputs(train_df)


# ============================================================
# entry point
# ============================================================

if __name__ == "__main__":
    main()
