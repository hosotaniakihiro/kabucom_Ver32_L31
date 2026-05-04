# ============================================================
# AI/monitor/build_ai_metrics.py
# STEP3-①-① AI パフォーマンス集計
# ------------------------------------------------------------
# ✔ ENTRYイベント × 約定結果を突合
# ✔ 勝率 / EV / 平均損益を算出
# ✔ ai_metrics.db に保存
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
import datetime as dt
import logging

logger = logging.getLogger(__name__)

# ============================================================
# DB
# ============================================================

AI_DATA_DIR = Path("AI/data")
ENTRY_DB = AI_DATA_DIR / "ai_entry_events.db"
TRADE_DB = AI_DATA_DIR / "trade_history.db"   # ← 既存DB
METRIC_DB = AI_DATA_DIR / "ai_metrics.db"

TABLE = "ai_metrics"


# ============================================================
# テーブル作成
# ============================================================

def ensure_table():
    conn = sqlite3.connect(METRIC_DB)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            date            TEXT,
            source          TEXT,
            trades          INTEGER,
            win_rate        REAL,
            avg_pnl         REAL,
            avg_pnl_pct     REAL,
            ev              REAL
        );
    """)
    conn.commit()
    conn.close()


# ============================================================
# メイン
# ============================================================

def main():

    ensure_table()

    # ENTRYイベント
    entry = pd.read_sql(
        "SELECT * FROM entry_events",
        sqlite3.connect(ENTRY_DB),
    )

    # 約定結果
    trade = pd.read_sql(
        "SELECT * FROM trade_history",
        sqlite3.connect(TRADE_DB),
    )

    if entry.empty or trade.empty:
        logger.warning("no data for ai metrics")
        return

    # symbol + 直近entry時刻でJOIN（簡易）
    df = entry.merge(
        trade,
        on="symbol",
        how="inner",
        suffixes=("_entry", "_trade"),
    )

    if df.empty:
        logger.warning("no matched entry/trade")
        return

    df["win"] = df["pnl"] > 0

    grouped = df.groupby("source")

    rows = []
    today = dt.date.today().isoformat()

    for source, g in grouped:
        trades = len(g)
        win_rate = g["win"].mean()
        avg_pnl = g["pnl"].mean()
        avg_pnl_pct = g["pnl_pct"].mean()
        ev = avg_pnl  # シンプルEV

        rows.append({
            "date": today,
            "source": source,
            "trades": trades,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "avg_pnl_pct": avg_pnl_pct,
            "ev": ev,
        })

    out = pd.DataFrame(rows)

    conn = sqlite3.connect(METRIC_DB)
    out.to_sql(TABLE, conn, if_exists="append", index=False)
    conn.close()

    logger.info(f"AI metrics saved ({len(out)})")


# ============================================================
if __name__ == "__main__":
    main()
