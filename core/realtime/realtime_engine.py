# ============================================================
# PATH: core/realtime/realtime_engine.py
# PRODUCTION-ULTRA-STABLE-REALTIME-ENGINE (FINAL VERSION)
# ------------------------------------------------------------
# ✔ 1分周期自動更新
# ✔ SQLite → DuckDB 差分ロード
# ✔ summary再構築（全再構築 or 差分フック）
# ✔ indicators再構築
# ✔ scoring再構築
# ✔ 3分 / 5分 TOP10表示
# ✔ 取引時間判定
# ✔ 例外耐性
# ✔ CPU負荷考慮
# ✔ 安全停止対応
# ✔ 実行時間詳細ログ
# ✔ MarketClose低負荷モード
# ✔ 将来差分更新対応フック
# ✔ GC安定化
# ✔ 本番安定設計
# ============================================================

from __future__ import annotations

import time
import logging
import datetime as dt
import traceback
import gc
import signal
import sys

from database.duckdb.manager import duck_manager
from database.duckdb.summary import rebuild_all_summaries
from database.duckdb.indicators import rebuild_all_indicators
from database.duckdb.scoring import rebuild_scoring

from core.realtime.sql_summary_printer import print_sql_top10

logger = logging.getLogger(__name__)

# ============================================================
# グローバル停止フラグ
# ============================================================

STOP_REQUESTED = False


def _handle_stop_signal(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    logger.warning("🛑 Stop signal received")


signal.signal(signal.SIGINT, _handle_stop_signal)
signal.signal(signal.SIGTERM, _handle_stop_signal)

# ============================================================
# 設定
# ============================================================

USE_DIFF_MODE = False  # 将来差分更新へ切替可能
PRINT_TOP10 = True     # 3m / 5m 表示フラグ


# ============================================================
# 取引時間判定（日本株）
# ============================================================

def is_market_open(now: dt.datetime) -> bool:

    if now.weekday() >= 5:
        return False

    morning_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    morning_end   = now.replace(hour=11, minute=30, second=0, microsecond=0)
    afternoon_start = now.replace(hour=12, minute=30, second=0, microsecond=0)
    afternoon_end   = now.replace(hour=15, minute=30, second=0, microsecond=0)

    return (
        morning_start <= now <= morning_end
        or afternoon_start <= now <= afternoon_end
    )


# ============================================================
# 実行時間ロガー
# ============================================================

def _log_elapsed(label: str, start_time: dt.datetime):
    elapsed = (dt.datetime.now() - start_time).total_seconds()
    logger.info(f"[TIMING] {label}: {elapsed:.3f}s")


# ============================================================
# 1回分処理
# ============================================================

def run_single_cycle():

    cycle_start = dt.datetime.now()
    logger.info("🚀 DuckDB cycle started")

    try:
        # ----------------------------------------------------
        # 1️⃣ SQLite → DuckDB 差分ロード
        # ----------------------------------------------------
        t0 = dt.datetime.now()
        duck_manager.load_today()
        _log_elapsed("Load SQLite→DuckDB", t0)

        # ----------------------------------------------------
        # 2️⃣ Summary
        # ----------------------------------------------------
        t0 = dt.datetime.now()
        rebuild_all_summaries()
        _log_elapsed("Rebuild Summary", t0)

        # ----------------------------------------------------
        # 3️⃣ Indicators
        # ----------------------------------------------------
        t0 = dt.datetime.now()
        rebuild_all_indicators()
        _log_elapsed("Rebuild Indicators", t0)

        # ----------------------------------------------------
        # 4️⃣ Scoring
        # ----------------------------------------------------
        t0 = dt.datetime.now()
        rebuild_scoring()
        _log_elapsed("Rebuild Scoring", t0)

        # ----------------------------------------------------
        # 5️⃣ TOP10表示
        # ----------------------------------------------------
        if PRINT_TOP10:
            print_sql_top10(3)
            print_sql_top10(5)

    except Exception:
        logger.error("❌ Error inside run_single_cycle")
        logger.error(traceback.format_exc())

    finally:
        gc.collect()

    _log_elapsed("Full Cycle", cycle_start)


# ============================================================
# リアルタイムループ
# ============================================================

def run_realtime_loop(interval_seconds: int = 60):

    logger.info("========================================")
    logger.info("🚀 DuckDB Realtime Engine Started")
    logger.info("========================================")

    while not STOP_REQUESTED:

        loop_start = dt.datetime.now()

        try:
            now = dt.datetime.now()

            if is_market_open(now):

                run_single_cycle()

                elapsed = (dt.datetime.now() - loop_start).total_seconds()
                sleep_time = max(1, interval_seconds - elapsed)

            else:
                logger.info("⏸ Market closed → standby mode")
                sleep_time = 120  # 閉場時低負荷

            logger.debug(f"Sleeping for {sleep_time:.2f}s")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.warning("🛑 Realtime engine stopped by user")
            break

        except Exception:
            logger.error("❌ Realtime engine error")
            logger.error(traceback.format_exc())
            time.sleep(5)

    logger.info("🛑 Realtime Engine terminated safely")

    try:
        duck_manager.close()
    except Exception:
        logger.exception("DuckDB close failed")


# ============================================================
# エントリーポイント
# ============================================================

if __name__ == "__main__":

    try:
        run_realtime_loop(interval_seconds=60)

    except Exception:
        logger.error("Fatal error in realtime engine")
        logger.error(traceback.format_exc())
        sys.exit(1)