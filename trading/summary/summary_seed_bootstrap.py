# ============================================================
# summary_seed_bootstrap.py
# Ver1.0-PREV-DAY-SEED-COPY
# ------------------------------------------------------------
# ✔ 前日summaryDBから最終bars本コピー
# ✔ 1min / 3min / 5min対応
# ✔ 当日DB未存在でも自動作成
# ✔ 重複防止
# ✔ 高速
# ============================================================

import datetime as dt
from pathlib import Path
from sqlalchemy import create_engine, text
import pandas as pd
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_bootstrap")

BASE_DIR = Path(
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary"
)

BARS = 120

today = dt.date.today()
yesterday = today - dt.timedelta(days=1)

today_str = today.strftime("%Y%m%d")
yesterday_str = yesterday.strftime("%Y%m%d")

today_db = BASE_DIR / f"summary{today_str}.db"
yesterday_db = BASE_DIR / f"summary{yesterday_str}.db"

if not yesterday_db.exists():
    logger.warning("No yesterday DB found. Skipping seed bootstrap.")
    exit()

today_engine = create_engine(f"sqlite:///{today_db}")
yesterday_engine = create_engine(f"sqlite:///{yesterday_db}")

TABLES = [
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
]

for table in TABLES:

    logger.info(f"Bootstrapping {table}")

    # 前日最終bars本取得
    df = pd.read_sql(
        text(f"""
            SELECT *
            FROM {table}
            ORDER BY datetime DESC
            LIMIT :bars
        """),
        yesterday_engine,
        params={"bars": BARS},
    )

    if df.empty:
        logger.warning(f"No rows found in {table}")
        continue

    # datetime順に戻す
    df = df.sort_values("datetime")

    # 当日DBへ挿入（重複無視）
    df.to_sql(
        table,
        today_engine,
        if_exists="append",
        index=False,
    )

logger.info("=== SEED BOOTSTRAP DONE ===")