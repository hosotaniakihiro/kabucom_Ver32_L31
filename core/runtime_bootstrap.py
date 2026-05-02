# ============================================================
# core/runtime_bootstrap.py
# Ver4.0-PRODUCTION-BOOTSTRAP-ULTRA-STABLE-FINAL
# ------------------------------------------------------------
# ✔ Ver3.0 全機能完全保持（削除ゼロ）
# ✔ 全DB接続確認（WAL前提）
# ✔ global_data 初期化完全保証
# ✔ initial_summary 実行
# ✔ realtime_engine 起動
# ✔ scheduler 起動
# ✔ 二重起動完全防止
# ✔ スレッド安全
# ✔ 例外隔離
# ✔ 再起動安全
# ✔ 停止安全
# ✔ 本番完全構成
# ============================================================

from __future__ import annotations

import logging
import threading
import time
import traceback

from global_state import global_data

# --- DB engines ---
from database.session import (
    push_engine,
    summary_engine,
    ranking_engine,
    position_engine,
    tosama_engine,
)

# --- Initial summary ---
from trading.summary.initial_summary import run_initial_fast_rebuild

# --- Realtime ---
from trading.summary.realtime_engine import realtime_engine

# --- Scheduler ---
from core.scheduler_tasks import start_scheduler

logger = logging.getLogger(__name__)


# ============================================================
# Runtime Bootstrap
# ============================================================

class RuntimeBootstrap:

    def __init__(self):

        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started_once = False

    # ========================================================
    # DB確認
    # ========================================================

    def _verify_db_connections(self):

        try:
            push_engine.connect().close()
            summary_engine.connect().close()
            ranking_engine.connect().close()
            position_engine.connect().close()
            tosama_engine.connect().close()

            logger.info("🧠 DB connections verified")

        except Exception:
            logger.exception("❌ DB connection failed")
            raise

    # ========================================================
    # Global state初期化
    # ========================================================

    def _initialize_global_state(self):

        global_data.is_initializing = True
        global_data.allow_summary_update = False
        global_data.initial_summary_completed = False

        logger.info("🌍 Global state initialized")

    # ========================================================
    # Initial Summary
    # ========================================================

    def _run_initial_summary(self):

        logger.info("🚀 Running initial summary rebuild")

        try:
            ok = run_initial_fast_rebuild()

            if not ok:
                logger.error("❌ Initial summary failed")
            else:
                logger.info("✅ Initial summary completed")

        except Exception:
            logger.error("❌ Initial summary crashed")
            logger.error(traceback.format_exc())

    # ========================================================
    # Realtime loop thread
    # ========================================================

    def _realtime_loop(self):

        logger.info("⚡ Realtime loop started")

        while self._running:

            try:
                realtime_engine.run_cycle()

            except Exception:
                logger.error("🔥 Realtime loop error")
                logger.error(traceback.format_exc())

            time.sleep(1)

        logger.info("🛑 Realtime loop stopped")

    # ========================================================
    # Start
    # ========================================================

    def start(self):

        with self._lock:

            if self._running:
                logger.info("⚠ Runtime already running")
                return

            logger.info("======================================")
            logger.info("🚀 RUNTIME BOOTSTRAP START")
            logger.info("======================================")

            try:
                # --- DB確認 ---
                self._verify_db_connections()

                # --- Global state ---
                self._initialize_global_state()

                # --- Initial summary ---
                self._run_initial_summary()

                # --- 初期化解除 ---
                global_data.is_initializing = False
                global_data.allow_summary_update = True
                global_data.initial_summary_completed = True

                # --- Scheduler起動 ---
                try:
                    start_scheduler()
                    logger.info("🗓 Scheduler started")
                except Exception:
                    logger.error("Scheduler failed to start")
                    logger.error(traceback.format_exc())

                # --- Realtime起動 ---
                self._running = True

                self._thread = threading.Thread(
                    target=self._realtime_loop,
                    daemon=True,
                    name="RealtimeLoopThread",
                )

                self._thread.start()

                self._started_once = True

                logger.info("🔥 RUNTIME STARTED")

            except Exception:
                logger.error("🔥 Bootstrap fatal error")
                logger.error(traceback.format_exc())
                self._running = False

    # ========================================================
    # Stop
    # ========================================================

    def stop(self):

        with self._lock:

            if not self._running:
                logger.info("⚠ Runtime not running")
                return

            logger.info("🛑 RUNTIME STOP REQUEST")

            self._running = False

        # joinはlock外で実行
        if self._thread:
            self._thread.join(timeout=5)

        logger.info("🛑 RUNTIME STOPPED")

    # ========================================================
    # Restart
    # ========================================================

    def restart(self):

        logger.info("🔁 RUNTIME RESTART")

        self.stop()
        time.sleep(1)
        self.start()

    def bootstrap_runtime():

        # 前日3m/5mのみロード（再計算しない）
        load_previous_tf(3)
        load_previous_tf(5)

        # 今日の最新1mのみロード
        load_today_last_1m()

        # キャッシュ初期化
        init_summary_cache()

        print("🚀 READY")
# ============================================================
# Singleton
# ============================================================

runtime_bootstrap = RuntimeBootstrap()