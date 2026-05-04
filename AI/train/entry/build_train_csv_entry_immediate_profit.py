# ============================================================
# build_train_csv_entry_immediate_profit.py
# ENTRY直後 含み益AI 学習CSV生成
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import timedelta

from config.paths import get_path

# ------------------------------------------------------------
# 設定（paths.py 経由）
# ------------------------------------------------------------
SUMMARY_DIR: Path = get_path("runtime_summary")
PUSH_DIR: Path = get_path("raw_push")
OUTPUT_CSV: Path = get_path("ai_train_data") / "ai_train_entry_immediate_profit.csv"

LOOKAHEAD_SECONDS = 30

FEATURE_COLUMNS = [
    "volume_speed",
    "price_velocity",
    "spread",
    "distance_from_vwap",
    "breakout_strength",
    "orderbook_imbalance",
]

# ------------------------------------------------------------
def load_push_prices(con, symbol, entry_dt):
    """
    entry_dt 以降 30秒以内の最終価格を取得
    """
    df = pd.read_sql(
        """
        SELECT datetime, price
        FROM stream_data
        WHERE symbol=?
          AND datetime>=?
          AND datetime<=?
        ORDER BY datetime
        """,
        con,
        params=[
            symbol,
            entry_dt,
            entry_dt + timedelta(seconds=LOOKAHEAD_SECONDS),
        ],
        parse_dates=["datetime"],
    )
    if df.empty:
        return None
    return float(df["price"].iloc[-1])


# ------------------------------------------------------------
def process_one_day(summary_db: Path, push_db: Path):
    rows = []

    with sqlite3.connect(summary_db) as con_sum, \
         sqlite3.connect(push_db) as con_push:

        df = pd.read_sql(
            """
            SELECT
                datetime,
                symbol,
                close AS entry_price,
                volume_speed,
                price_velocity,
                spread,
                distance_from_vwap,
                breakout_strength,
                orderbook_imbalance
            FROM stock_summary_1min
            WHERE volume_speed IS NOT NULL
            """,
            con_sum,
            parse_dates=["datetime"],
        )

        for _, r in df.iterrows():
            price_30s = load_push_prices(
                con_push, r["symbol"], r["datetime"]
            )
            if price_30s is None:
                continue

            label = 1 if price_30s > r["entry_price"] else 0

            rows.append({
                "symbol": r["symbol"],
                "entry_price": r["entry_price"],
                **{c: r[c] for c in FEATURE_COLUMNS},
                "label_profit_30s": label,
            })

    return rows


# ------------------------------------------------------------
def main():
    all_rows = []

    for summary_db in sorted(SUMMARY_DIR.glob("summary*.db")):
        trade_date = summary_db.stem.replace("summary", "")
        push_db = PUSH_DIR / f"push{trade_date}.db"

        if not push_db.exists():
            continue

        print(f"📌 処理中 {trade_date}")
        all_rows.extend(process_one_day(summary_db, push_db))

    df = pd.DataFrame(all_rows)
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"✅ 出力完了: {OUTPUT_CSV} rows={len(df)}")


# ------------------------------------------------------------
if __name__ == "__main__":
    main()
