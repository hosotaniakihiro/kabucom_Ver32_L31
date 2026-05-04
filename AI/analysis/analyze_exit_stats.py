# ============================================================
# AI/analysis/analyze_exit_stats.py
# Ver1.0.0-FINAL-EXIT-ANALYZER
# ------------------------------------------------------------
# ✔ trade_exit_stats を分析
# ✔ EXIT の良否・取り逃し量を数値化
# ✔ 戦略改善・EXIT AI 教師生成の土台
# ✔ DB 書き込みなし（完全 read-only）
# ============================================================

from __future__ import annotations

import sqlite3
import pandas as pd
from pathlib import Path
from typing import Optional

from config.paths import get_path


# ============================================================
# 設定
# ============================================================

DB_PATH: Path = get_path("position_db")   # positions.db
TABLE = "trade_exit_stats"

# 分析パラメータ（経験則）
GOOD_EXIT_MIN_PNL_PCT = 0.15       # +0.15%以上は成功
MISS_THRESHOLD_PCT    = 0.30       # 30%以上の取り逃しは「悪いEXIT」
SHORT_HOLD_SEC        = 10         # 10秒未満はスキャルプ扱い


# ============================================================
# ロード
# ============================================================

def load_exit_stats(limit: Optional[int] = None) -> pd.DataFrame:
    sql = f"SELECT * FROM {TABLE}"
    if limit:
        sql += f" ORDER BY created_at DESC LIMIT {int(limit)}"

    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql(sql, conn)

    if df.empty:
        raise RuntimeError("trade_exit_stats is empty")

    return df


# ============================================================
# 基本指標追加
# ============================================================

def enrich_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # 実現損益率
    df["pnl_pct"] = (df["exit_price"] - df["entry_price"]) / df["entry_price"] * 100
    df.loc[df["side"] == "SELL", "pnl_pct"] *= -1

    # 取り逃し率
    df["miss_pct"] = df["mfe_pct"] - df["pnl_pct"]

    # HOLD 時間区分
    df["hold_bucket"] = pd.cut(
        df["holding_seconds"],
        bins=[0, 10, 30, 60, 180, 999999],
        labels=["<=10s", "10-30s", "30-60s", "1-3m", ">3m"],
    )

    return df


# ============================================================
# EXIT 評価ラベル
# ============================================================

def classify_exit(row: pd.Series) -> str:
    """
    EXIT の評価（人間の直感をルール化）
    """

    # 強制系は基本 NG
    if row["exit_reason"] in ("HARD_STOP",):
        return "BAD_EXIT"

    # 利益が十分取れている
    if row["pnl_pct"] >= GOOD_EXIT_MIN_PNL_PCT:
        return "GOOD_EXIT"

    # 大きな取り逃し
    if row["miss_pct"] >= MISS_THRESHOLD_PCT:
        return "TOO_EARLY_EXIT"

    # 短期スキャルプ成功
    if row["holding_seconds"] <= SHORT_HOLD_SEC and row["pnl_pct"] > 0:
        return "GOOD_SCALP"

    return "NEUTRAL_EXIT"


# ============================================================
# サマリー出力
# ============================================================

def print_summary(df: pd.DataFrame) -> None:
    print("\n========== EXIT SUMMARY ==========\n")

    print("▶ 件数:", len(df))
    print("▶ 勝率:", (df["pnl_pct"] > 0).mean().round(3))
    print("▶ 平均 PnL%:", df["pnl_pct"].mean().round(3))
    print("▶ 平均 MFE%:", df["mfe_pct"].mean().round(3))
    print("▶ 平均 MAE%:", df["mae_pct"].mean().round(3))

    print("\n--- EXIT REASON ---")
    print(df["exit_reason"].value_counts())

    print("\n--- EXIT LABEL ---")
    print(df["exit_label"].value_counts())

    print("\n--- HOLD TIME ---")
    print(df["hold_bucket"].value_counts())


# ============================================================
# 取り逃しランキング
# ============================================================

def print_top_missed(df: pd.DataFrame, n: int = 10) -> None:
    print("\n========== TOP MISSED EXITS ==========\n")

    cols = [
        "symbol",
        "side",
        "exit_reason",
        "pnl_pct",
        "mfe_pct",
        "miss_pct",
        "holding_seconds",
    ]

    top = (
        df.sort_values("miss_pct", ascending=False)
        .head(n)[cols]
        .round(3)
    )

    print(top.to_string(index=False))


# ============================================================
# メイン
# ============================================================

def main(limit: Optional[int] = None) -> None:
    df = load_exit_stats(limit=limit)
    df = enrich_metrics(df)

    # 評価ラベル付与
    df["exit_label"] = df.apply(classify_exit, axis=1)

    print_summary(df)
    print_top_missed(df, n=15)


# ============================================================
# entry point
# ============================================================

if __name__ == "__main__":
    main(limit=2000)
