# ============================================================
# AI/train/build_train_csv_entry.py
# ------------------------------------------------------------
# ENTRYイベント + 価格DB から学習用CSVを生成
# ============================================================

import sqlite3
import json
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# ===================== 設定 =====================

ENTRY_DB = Path("AI/data/ai_entry_events.db")
PRICE_DB = Path("Y:/stock_price_data/push_5s.db")  # ← 環境に合わせて
OUT_DIR = Path("AI/train")
OUT_DIR.mkdir(exist_ok=True)

LOOKAHEAD_SEC = 60
TARGET_PCT = 0.002

# ===================== 価格取得 =====================

def load_prices(symbol: str, start: datetime, end: datetime):
    con = sqlite3.connect(PRICE_DB)
    df = pd.read_sql(
        """
        SELECT datetime, price
        FROM prices_5s
        WHERE symbol = ?
          AND datetime BETWEEN ? AND ?
        ORDER BY datetime
        """,
        con,
        params=(symbol, start.isoformat(), end.isoformat()),
    )
    con.close()
    return df

# ===================== メイン =====================

def main():

    con = sqlite3.connect(ENTRY_DB)
    entries = pd.read_sql("SELECT * FROM entry_events", con)
    con.close()

    rows = []

    for _, e in entries.iterrows():

        t0 = datetime.fromisoformat(e["datetime"])
        t1 = t0 + timedelta(seconds=LOOKAHEAD_SEC)

        symbol = e["symbol"]
        side = e["side"]

        feats = json.loads(e["features_json"])
        entry_price = feats.get("entry_price")

        if not entry_price:
            continue

        prices = load_prices(symbol, t0, t1)
        if prices.empty:
            continue

        if side == "BUY":
            max_p = prices["price"].max()
            profit = max_p / entry_price - 1
        else:
            min_p = prices["price"].min()
            profit = entry_price / min_p - 1

        y = 1 if profit >= TARGET_PCT else 0

        row = {
            **feats,
            "side": side,
            "y": y,
            "profit": profit,
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    out = OUT_DIR / "train_entry.csv"
    df.to_csv(out, index=False)

    print(f"✅ saved {out} rows={len(df)}")


if __name__ == "__main__":
    main()
