# ============================================================
# yahoo_finance_data_processor_kanshi_meigara_only_1m_5m.py
# ------------------------------------------------------------
# ・Yahoo Finance データ（1m / 5m）を処理
# ・監視銘柄（kanshi_meigara）のみ対象
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
import logging

from config.paths import get_path

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# paths.py 経由
# ------------------------------------------------------------
RAW_YAHOO_DIR: Path = get_path("raw_yahoo")
SUMMARY_DIR: Path = get_path("runtime_summary")


# ------------------------------------------------------------
def load_kanshi_symbols() -> set[str]:
    """
    監視銘柄一覧を取得
    （既存仕様：summary DB もしくは別途生成済み前提）
    """
    kanshi_file = RAW_YAHOO_DIR / "kanshi_symbols.csv"
    if not kanshi_file.exists():
        logger.warning(f"⚠ kanshi symbol list not found: {kanshi_file}")
        return set()

    try:
        df = pd.read_csv(kanshi_file)
        return set(df["symbol"].astype(str))
    except Exception:
        logger.exception("❌ failed to load kanshi symbols")
        return set()


# ------------------------------------------------------------
def process_yahoo_data(trade_date: str):
    """
    Yahoo の 1分・5分データを summary DB に反映
    """
    kanshi_symbols = load_kanshi_symbols()
    if not kanshi_symbols:
        logger.warning("⚠ kanshi symbol empty")
        return

    # Yahoo 生 CSV
    yahoo_csv_1m = RAW_YAHOO_DIR / f"yahoo_1m_{trade_date}.csv"
    yahoo_csv_5m = RAW_YAHOO_DIR / f"yahoo_5m_{trade_date}.csv"

    if not yahoo_csv_1m.exists() and not yahoo_csv_5m.exists():
        logger.warning("⚠ yahoo csv not found")
        return

    # summary DB
    summary_db = SUMMARY_DIR / f"summary{trade_date}.db"
    if not summary_db.exists():
        logger.warning(f"⚠ summary DB not found: {summary_db}")
        return

    con = sqlite3.connect(summary_db)

    try:
        # ----------------------------------------------------
        # 1分足
        # ----------------------------------------------------
        if yahoo_csv_1m.exists():
            df1 = pd.read_csv(yahoo_csv_1m)
            df1["symbol"] = df1["symbol"].astype(str)
            df1 = df1[df1["symbol"].isin(kanshi_symbols)]

            if not df1.empty:
                df1.to_sql(
                    "stock_summary_1min",
                    con,
                    if_exists="append",
                    index=False,
                )
                logger.info(f"✅ yahoo 1m inserted: {len(df1)}")

        # ----------------------------------------------------
        # 5分足
        # ----------------------------------------------------
        if yahoo_csv_5m.exists():
            df5 = pd.read_csv(yahoo_csv_5m)
            df5["symbol"] = df5["symbol"].astype(str)
            df5 = df5[df5["symbol"].isin(kanshi_symbols)]

            if not df5.empty:
                df5.to_sql(
                    "stock_summary_5min",
                    con,
                    if_exists="append",
                    index=False,
                )
                logger.info(f"✅ yahoo 5m inserted: {len(df5)}")

    except Exception:
        logger.exception("❌ yahoo data processing failed")

    finally:
        con.close()


# ------------------------------------------------------------
# entry point
# ------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python yahoo_finance_data_processor_kanshi_meigara_only_1m_5m.py YYYYMMDD")
        sys.exit(1)

    process_yahoo_data(sys.argv[1])
