# ============================================================
# summary_scheduler.py（Ver17_8 — サマリー→エントリー直列実行 完全版）
# ============================================================

import schedule
import logging
import threading
import time
import traceback

from trading.summary.scheduled_summary import run_scheduled_summary
from trading.summary.summary_controller import summary_controller
from trading.handlers.entry_controller import run_entry_pipeline
from trading.handlers.exit_handler import run_exit_pipeline

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# 共通サマリー → エントリー 直列実行
# ------------------------------------------------------------
def run_summary_and_entry(interval: int):
    try:
        logger.info(f"🔵 {interval}min サマリー開始")

        # ① interval サマリー生成
        df_new = run_scheduled_summary(interval)
        if df_new is None or df_new.empty:
            logger.warning(f"{interval}min summary empty")
            return

        # ② merged_summary 更新
        df = summary_controller.update_from_scheduled(interval, df_new)
        if df is None or df.empty:
            logger.warning(f"{interval}min merged empty")
            return

        # ③ エントリー判定（🔥 ここで直列実行）
        logger.info(f"🚀 エントリー判定開始 interval={interval}")
        run_entry_pipeline(f"{interval}min")

        logger.info(f"✅ {interval}min サマリー+エントリー完了")

    except Exception:
        logger.error(f"❌ run_summary_and_entry({interval}) 失敗")
        logger.error(traceback.format_exc())


# ------------------------------------------------------------
# スケジューラ登録
# ------------------------------------------------------------
def setup_summary_schedule():
    logger.info("🕒 setup_summary_schedule START")

    # 🔹 1min → 60 秒ごと
    schedule.every(1).minutes.do(lambda: run_summary_and_entry(1))

    # 🔹 3min → 3分ごと
    schedule.every(3).minutes.do(lambda: run_summary_and_entry(3))

    # 🔹 5min → 5分ごと
    schedule.every(5).minutes.do(lambda: run_summary_and_entry(5))

    # 🔹 EXIT（独立）
    schedule.every(30).seconds.do(run_exit_pipeline)

    # 🔹 スケジュールループ
    def loop():
        logger.info("🌀 スケジューラースレッド開始")
        while True:
            try:
                schedule.run_pending()
            except Exception:
                logger.error("❌ run_pending 失敗")
                logger.error(traceback.format_exc())
            time.sleep(1)

    threading.Thread(target=loop, daemon=True).start()
    logger.info("✅ setup_summary_schedule OK")
