# ============================================================
# AI/build_train_csv_entry_mode_selector.py
# ------------------------------------------------------------
# ✔ 仮想ENTRY（BREAKOUT / PULLBACK）両方を生成
# ✔ label = ENTRY30秒後に含み益か
# ✔ summary + push DB から自動生成
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import timedelta

from config.paths import get_path

# ============================================================
# 設定（paths.py 経由）
# ============================================================

SUMMARY_DIR: Path = get_path("runtime_summary")
PUSH_DIR: Path = get_path("raw_push")
OUTPUT_CSV: Path = get_path("ai_train_data") / "train_entry_mode_selector.csv"

LOOKAHEAD_SECONDS = 30

COMMON_FEATURES = [
    "volume_speed",
    "price_velocity",
    "spread",
    "distance_from_vwap",
    "orderbook_imbalance",
    "trend_strength",
]

BREAKOUT_FEATURES = [
    "high_break_distance",
    "range_compression",
    "recent_high_count",
]

PULLBACK_FEATURES = [
    "pullback_depth",
    "vwap_touch_count",
    "ma_support_strength",
]


# ============================================================
def _load_price_after(con, symbol, t0):
    """
    t0 から 30秒以内の最終約定価格
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
            t0,
            t0 + timedelta(seconds=LOOKAHEAD_SECONDS),
        ],
        parse_dates=["datetime"],
    )

    if df.empty:
        return None

    return float(df["price"].iloc[-1])


# ============================================================
def _process_one_day(summary_db: Path, push_db: Path):
    rows = []

    with sqlite3.connect(summary_db) as con_sum, \
         sqlite3.connect(push_db) as con_push:

        df = pd.read_sql(
            """
            SELECT *
            FROM stock_summary_1min
            WHERE entry_decision IN ('BUY','SELL')
            """,
            con_sum,
            parse_dates=["datetime"],
        )

        if df.empty:
            return rows

        for _, r in df.iterrows():
            symbol = r["symbol"]
            t0 = r["datetime"]
            entry_price = r["close_price"]

            price_30s = _load_price_after(con_push, symbol, t0)
            if price_30s is None:
                continue

            label = 1 if price_30s > entry_price else 0

            # -----------------------------
            # BREAKOUT 仮想ENTRY
            # -----------------------------
            rows.append({
                "symbol": symbol,
                "mode": "BREAKOUT",
                "entry_price": entry_price,
                **{k: r.get(k) for k in COMMON_FEATURES},
                **{k: r.get(k) for k in BREAKOUT_FEATURES},
                "label_profit_30s": label,
            })

            # -----------------------------
            # PULLBACK 仮想ENTRY
            # -----------------------------
            rows.append({
                "symbol": symbol,
                "mode": "PULLBACK",
                "entry_price": entry_price,
                **{k: r.get(k) for k in COMMON_FEATURES},
                **{k: r.get(k) for k in PULLBACK_FEATURES},
                "label_profit_30s": label,
            })

    return rows


# ============================================================
def main():
    all_rows = []

    for summary_db in sorted(SUMMARY_DIR.glob("summary*.db")):
        trade_date = summary_db.stem.replace("summary", "")
        push_db = PUSH_DIR / f"push{trade_date}.db"

        if not push_db.exists():
            continue

        print(f"📌 processing {trade_date}")
        all_rows.extend(_process_one_day(summary_db, push_db))

    df = pd.DataFrame(all_rows)
    df = df.dropna()

    OUTPUT_CSV.parent.mkdir(exist_ok=True, parents=True)
    df.to_csv(OUTPUT_CSV, index=False)

    print(f"✅ CSV generated: {OUTPUT_CSV} rows={len(df)}")


# ============================================================
if __name__ == "__main__":
    main()
