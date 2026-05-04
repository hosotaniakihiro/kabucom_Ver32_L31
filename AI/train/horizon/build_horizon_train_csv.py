# ============================================================
# build_horizon_train_csv.py
# horizon（30/60/120秒）EXIT 学習CSV生成
# ------------------------------------------------------------
# EXIT時点 → 未来価格を見て「今 EXIT して正解だったか」
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
from pathlib import Path
import pandas as pd
import datetime as dt

from config.paths import get_path

# ============================================================
# PATH（paths.py 経由）
# ============================================================

PUSH_DIR: Path = get_path("runtime_push")
EXIT_DB: Path  = get_path("runtime_exit") / "exit_log.db"

OUTPUT: Path = get_path("ai_train_data") / "horizon" / "horizon_train.csv"

# 未来を何秒見るか
HORIZONS = [30, 60, 120]


# ============================================================
def get_future_price(symbol: str, base_dt: dt.datetime, seconds: int):
    """
    EXIT 時点から seconds 秒後までの最終価格
    """
    push_db = PUSH_DIR / f"push{base_dt:%Y%m%d}.db"
    if not push_db.exists():
        return None

    with sqlite3.connect(push_db) as con:
        df = pd.read_sql(
            """
            SELECT price
            FROM stream_data
            WHERE symbol=?
              AND datetime>=?
              AND datetime<=?
            ORDER BY datetime
            """,
            con,
            params=[
                symbol,
                base_dt,
                base_dt + dt.timedelta(seconds=seconds),
            ],
        )

    if df.empty:
        return None

    return float(df.iloc[-1]["price"])


# ============================================================
def main():

    rows = []

    if not EXIT_DB.exists():
        print(f"❌ Exit DB not found: {EXIT_DB}")
        return

    with sqlite3.connect(EXIT_DB) as con:
        exit_df = pd.read_sql(
            """
            SELECT
                symbol,
                exit_time,
                exit_price,
                pnl_pct
            FROM exit_log
            """,
            con,
            parse_dates=["exit_time"],
        )

    if exit_df.empty:
        print("⚠ ExitLog empty")
        return

    for _, r in exit_df.iterrows():
        row = {
            "profit_rate": r["pnl_pct"],
        }

        for h in HORIZONS:
            future_price = get_future_price(
                r["symbol"],
                r["exit_time"],
                h,
            )

            if future_price is None:
                row[f"label_{h}"] = None
                continue

            # EXIT後に不利に動いた → EXITは正解 = 1
            diff_pct = (future_price - r["exit_price"]) / r["exit_price"] * 100
            row[f"label_{h}"] = 1 if diff_pct < -0.2 else 0

        rows.append(row)

    df = pd.DataFrame(rows).dropna()

    if df.empty:
        print("⚠ horizon train CSV empty")
        return

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT, index=False)

    print("=" * 60)
    print("✅ HORIZON TRAIN CSV GENERATED")
    print(f" rows : {len(df)}")
    print(f" path : {OUTPUT}")
    print("=" * 60)


if __name__ == "__main__":
    main()
