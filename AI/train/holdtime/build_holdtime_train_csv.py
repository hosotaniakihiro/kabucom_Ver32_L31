# ============================================================
# AI/train/holdtime/build_holdtime_train_csv.py
# HOLDTIME AI 学習CSV生成
# ------------------------------------------------------------
# EXITより前の状態 → 実際に EXIT するまで残った時間（秒）を学習
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
from pathlib import Path
import pandas as pd
from datetime import timedelta

from config.paths import get_path

# ============================================================
# PATH（paths.py 経由）
# ============================================================

SUMMARY_DIR: Path = get_path("runtime_summary")
EXIT_DB: Path     = get_path("runtime_exit") / "exit_log.db"

OUTPUT: Path = get_path("ai_train_data") / "holdtime" / "holdtime_train.csv"

# 何秒前まで遡って学習データを作るか
LOOKBACK_SECONDS = [5, 10, 20, 30, 45, 60]


def main():

    rows = []

    # --------------------------------------------------------
    # ExitLog 読み込み
    # --------------------------------------------------------
    if not EXIT_DB.exists():
        print(f"❌ Exit DB not found: {EXIT_DB}")
        return

    with sqlite3.connect(EXIT_DB) as con:
        exit_df = pd.read_sql(
            """
            SELECT
                symbol,
                exit_time,
                pnl_pct,
                holding_seconds
            FROM exit_log
            """,
            con,
            parse_dates=["exit_time"],
        )

    if exit_df.empty:
        print("⚠ ExitLog empty")
        return

    # --------------------------------------------------------
    # EXIT前スナップショットを複数生成
    # --------------------------------------------------------
    for _, r in exit_df.iterrows():
        symbol = r["symbol"]
        exit_dt = r["exit_time"]

        summary_db = SUMMARY_DIR / f"summary{exit_dt:%Y%m%d}.db"
        if not summary_db.exists():
            continue

        with sqlite3.connect(summary_db) as con:
            for sec in LOOKBACK_SECONDS:
                snap_time = exit_dt - timedelta(seconds=sec)

                snap = pd.read_sql(
                    """
                    SELECT
                        volume_speed,
                        volatility,
                        trend_strength
                    FROM stock_summary_1min
                    WHERE symbol=?
                      AND datetime<=?
                    ORDER BY datetime DESC
                    LIMIT 1
                    """,
                    con,
                    params=[symbol, snap_time],
                )

                if snap.empty:
                    continue

                rows.append({
                    # --- 特徴量 ---
                    "profit_rate": r["pnl_pct"],
                    "drawdown_rate": min(r["pnl_pct"], 0),
                    "volume_speed": snap.iloc[0]["volume_speed"],
                    "volatility": snap.iloc[0]["volatility"],
                    "trend_strength": snap.iloc[0]["trend_strength"],
                    "hold_seconds": max(int(r["holding_seconds"] - sec), 0),

                    # --- 教師ラベル ---
                    "remaining_hold_seconds": sec,
                })

    df = pd.DataFrame(rows)

    if df.empty:
        print("⚠ HOLDTIME train CSV empty")
        return

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT, index=False)

    print("=" * 60)
    print("✅ HOLDTIME TRAIN CSV GENERATED")
    print(f" rows : {len(df)}")
    print(f" path : {OUTPUT}")
    print("=" * 60)


if __name__ == "__main__":
    main()
