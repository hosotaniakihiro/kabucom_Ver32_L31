# ============================================================
# AI/build_training_csv.py
# ------------------------------------------------------------
# ✔ summary DB から AI 学習用 CSV を生成
# ✔ MTF / cluster / score 系対応
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
import logging

from config.paths import get_path

logger = logging.getLogger(__name__)

# ============================================================
# paths.py 経由
# ============================================================

SUMMARY_DIR: Path = get_path("runtime_summary")
OUT_DIR: Path = get_path("ai_train_data")
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_CSV: Path = OUT_DIR / "training_data.csv"


# ============================================================
def load_one_db(db_path: Path) -> pd.DataFrame:
    """
    単一 summary DB から学習データ抽出
    """
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql(
            """
            SELECT
                symbol,
                datetime,
                cluster,
                interval,
                score_total,
                score_buy,
                score_sell,
                entry_decision,
                close_price,
                volume,
                vwap,
                rsi,
                macd,
                atr
            FROM stock_summary_1min
            """,
            con,
            parse_dates=["datetime"],
        )
    return df


# ============================================================
def main():
    dfs = []

    for db in sorted(SUMMARY_DIR.glob("summary*.db")):
        try:
            df = load_one_db(db)
            if not df.empty:
                dfs.append(df)
        except Exception as e:
            logger.warning(f"⚠ skip {db}: {e}")

    if not dfs:
        print("⚠ no data")
        return

    df = pd.concat(dfs, ignore_index=True)

    # --------------------------------------------------------
    # 既存仕様：ラベル（次バー上昇）
    # --------------------------------------------------------
    df = df.sort_values(["symbol", "datetime"])
    df["y"] = (
        df.groupby("symbol")["close_price"]
          .shift(-1) > df["close_price"]
    ).astype(int)

    df = df.dropna()

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print(f"✅ training csv generated: {OUT_CSV} rows={len(df)}")


# ============================================================
if __name__ == "__main__":
    main()
