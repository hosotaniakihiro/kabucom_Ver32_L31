# =========================================
# optional_main.py
# Version: PRODUCTION-STABLE-REV4-REBUILD-SYMBOL-MAP-AFTER-OPTIONAL-LOAD
# =========================================
# ・optional 系 日次バッチのエントリポイント
# ・paths.py 前提
# ・DB migrate → ingest を起動時に1回だけ実行
# ・runtime（summary / ranking）とは別プロセスで実行する
# ・optional_data を global_data にロード
# ・日足DB由来のMA/MTFを global_data.daily_mtf_df にロードし、AI判定前merge patchを導入
#
# REV4:
#   - main.py では build_symbol_name_map() が optional_main() より前に呼ばれていたため、
#     optional_data 未ロード状態で symbol_name_map が空になることがあった。
#   - optional_data ロード直後に symbol_name_map を再構築し、SUMMARY TOP10 の銘柄名表示を復旧する。
# =========================================

from __future__ import annotations

import logging
import os
import sys
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
# env helpers
# ---------------------------------
def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _is_database_process() -> bool:
    return any(
        _env_bool(name, False)
        for name in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        )
    )


def _light_mode_enabled() -> bool:
    if _env_bool("OPTIONAL_LIGHT_MODE", False):
        return True
    if _env_bool("OPTIONAL_SKIP_INGEST", False):
        return True
    if _is_main_py_process() and not _is_database_process() and not _env_bool("OPTIONAL_RUN_INGEST_IN_MAIN", False):
        return True
    return False


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
from optional.db.reader import load_optional_dataframe


# ---------------------------------
# symbol map runtime boot
# ---------------------------------
def rebuild_symbol_name_map_safe() -> None:
    """optional_data ロード後に銘柄名mapを再構築する。"""
    try:
        from core.bootstrap.load_symbol_map import build_symbol_name_map

        build_symbol_name_map()
        mp = getattr(global_data, "symbol_name_map", {})
        logger.warning(
            "[OPTIONAL BOOT] symbol_name_map rebuilt after optional load rows=%s optional_rows=%s",
            len(mp) if isinstance(mp, dict) else 0,
            len(getattr(global_data, "optional_data", [])) if getattr(global_data, "optional_data", None) is not None else 0,
        )
    except Exception:
        logger.exception("[OPTIONAL BOOT] rebuild symbol_name_map failed (continue)")


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
    light_mode = _light_mode_enabled()

    logger.info("=" * 60)
    logger.info("🚀 OPTIONAL DAILY BOOT START")
    logger.info("📅 trading_date = %s", trade_date)
    logger.warning(
        "[OPTIONAL BOOT MODE] light_mode=%s is_main_py=%s is_database_process=%s run_ingest_in_main=%s",
        light_mode,
        _is_main_py_process(),
        _is_database_process(),
        os.getenv("OPTIONAL_RUN_INGEST_IN_MAIN"),
    )
    logger.info("=" * 60)

    if not light_mode:
        # -------------------------------------------------
        # ① OPTIONAL DB マイグレーション（ADD ONLY）
        # -------------------------------------------------
        try:
            logger.info("⏳ OPTIONAL DB migration start")
            migrate_optional_db()
            logger.info("✅ OPTIONAL DB migration completed")
        except Exception:
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
            raise
    else:
        logger.warning(
            "[OPTIONAL BOOT MODE] migrate/ingest skipped in light mode. main_database.py should refresh optional DB."
        )

    # -------------------------------------------------
    # ③ optional DB → DataFrame load
    # -------------------------------------------------
    try:
        logger.info("⏳ loading optional dataframe")
        optional_df: pd.DataFrame = load_optional_dataframe()
        if optional_df is None:
            optional_df = pd.DataFrame()
        global_data.optional_data = optional_df
        logger.info("✅ optional_data loaded rows=%d", len(optional_df))
    except Exception:
        logger.exception("❌ optional dataframe load failed")
        global_data.optional_data = pd.DataFrame()

    # -------------------------------------------------
    # ③-2 optional_data → symbol_name_map rebuild
    # -------------------------------------------------
    rebuild_symbol_name_map_safe()

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
