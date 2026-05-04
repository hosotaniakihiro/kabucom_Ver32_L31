# ============================================================
# build_train_data_real.py
# ------------------------------------------------------------
# ・実トレード結果から AI 学習用 CSV を生成
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path

from config.paths import get_path


# ------------------------------------------------------------
# paths.py 経由
# ------------------------------------------------------------
TRADE_DB: Path = get_path("raw_stock_data") / "trade_result.db"
OUT_DIR: Path = get_path("ai_train_data")
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_CSV: Path = OUT_DIR / "train_data_real.csv"


# ------------------------------------------------------------
def main():
    if not TRADE_DB.exists():
        raise FileNotFoundError(f"trade_result.db not found: {TRADE_DB}")

    con = sqlite3.connect(TRADE_DB)

    try:
        df = pd.read_sql("""
            SELECT
                symbol,
                summary_score,
                ranking_score,
                ai_prob,
                final_score,
                interval,
                pnl
            FROM trade_log
        """, con)
    finally:
        con.close()

    # 勝ち = 1 / 負け = 0
    df["y"] = (df["pnl"] > 0).astype(int)

    df.to_csv(OUT_CSV, index=False)
    print(f"✅ train_data_real.csv generated: {OUT_CSV}")


# ------------------------------------------------------------
# entry point
# ------------------------------------------------------------
if __name__ == "__main__":
    main()
