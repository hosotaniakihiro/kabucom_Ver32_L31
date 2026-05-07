# =========================================
# optional_main.py
# Version: PRODUCTION-STABLE-REV2-DAILY-MTF-RUNTIME-BOOT
# =========================================
# ・optional 系 日次バッチのエントリポイント
# ・paths.py 前提
# ・DB migrate → ingest を起動時に1回だけ実行
# ・runtime（summary / ranking）とは別プロセスで実行する
# ・optional_data を global_data にロード
# ・日足DB由来のMA/MTFを global_data.daily_mtf_df にロードし、AI判定前merge patchを導入
# =========================================

import logging
import traceback
from datetime import date, timedelta

import jpholiday
import pandas as pd

from global_state import global_data


# ---------------------------------
# logging
# ---------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ---------------------------------
# 営業日判定
# ---------------------------------
def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not jpholiday.is_holiday(d)


def get_latest_trading_day(d: date) -> str:

    while not is_trading_day(d):
        d -= timedelta(days=1)

    return d.strftime("%Y-%m-%d")


# ---------------------------------
# batch import
# ---------------------------------
from optional.db.migrate import migrate_optional_db
from optional.batch.ingest_all_optional_data import ingest_all


# optional DB reader
from optional.db.reader import load_optional_dataframe


# ---------------------------------
# daily MTF runtime boot
# ---------------------------------
def install_daily_mtf_runtime_safe() -> None:
    """
    旧main.pyで読んでいた日足DB由来のMA/MTFを復活させる。

    - 日足DBを global_data.daily_mtf_df へロード
    - summary AI runner へ渡る直前に daily MA/MTF を自動merge
    - 失敗しても optional/main boot は止めない
    """
    try:
        from trading.summary.mtf.daily_runtime_patch import install_daily_mtf_runtime_patch

        logger.info("⏳ DAILY MTF runtime patch install start")
        install_daily_mtf_runtime_patch()
        logger.info(
            "✅ DAILY MTF runtime patch installed rows=%s latest=%s db=%s table=%s",
            getattr(global_data, "daily_mtf_loaded_rows", None),
            getattr(global_data, "daily_mtf_latest_date", None),
            getattr(global_data, "daily_mtf_db_path", None),
            getattr(global_data, "daily_mtf_table", None),
        )

    except Exception:
        logger.exception("❌ DAILY MTF runtime patch install failed (continue)")


# ---------------------------------
# main
# ---------------------------------
def optional_main():

    trade_date = get_latest_trading_day(date.today())

    logger.info("=" * 60)
    logger.info("🚀 OPTIONAL DAILY BOOT START")
    logger.info("📅 trading_date = %s", trade_date)
    logger.info("=" * 60)

    # -------------------------------------------------
    # ① OPTIONAL DB マイグレーション（ADD ONLY）
    # -------------------------------------------------

    try:

        logger.info("⏳ OPTIONAL DB migration start")

        migrate_optional_db()

        logger.info("✅ OPTIONAL DB migration completed")

    except Exception:

        # migrate 失敗でも ingest を止めない（後方互換）

        logger.warning("⚠ OPTIONAL DB migration failed (continue)")

        traceback.print_exc()

    # -------------------------------------------------
    # ② OPTIONAL 系データ一括 ingest
    # -------------------------------------------------

    try:

        logger.info("⏳ OPTIONAL ingest start")

        ingest_all(trade_date)

        logger.info("✅ OPTIONAL ingest completed")

    except Exception:

        logger.error("❌ OPTIONAL ingest failed")

        traceback.print_exc()

        # system main / scheduler から呼ばれる前提 → ここは止める
        raise

    # -------------------------------------------------
    # ③ optional DB → DataFrame load
    # -------------------------------------------------

    try:

        logger.info("⏳ loading optional dataframe")

        optional_df: pd.DataFrame = load_optional_dataframe()

        if optional_df is None:
            optional_df = pd.DataFrame()

        global_data.optional_data = optional_df

        logger.info(
            "✅ optional_data loaded rows=%d",
            len(optional_df)
        )

    except Exception:

        logger.exception("❌ optional dataframe load failed")

        global_data.optional_data = pd.DataFrame()

    # -------------------------------------------------
    # ④ 日足DB → daily_mtf_df load + AI前merge patch
    # -------------------------------------------------
    install_daily_mtf_runtime_safe()

    logger.info("=" * 60)
    logger.info("🎉 OPTIONAL DAILY BOOT COMPLETED")
    logger.info("=" * 60)


# ---------------------------------
# entry point
# ---------------------------------
if __name__ == "__main__":

    optional_main()
