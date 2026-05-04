# ============================================================
# core/runtime_manager.py
# Ver26.3-FINAL-SCHEDULER-LOOP + SUMMARY + RANKING-INIT + YAHOO-DELEGATED
# ------------------------------------------------------------
# ✔ system_startup() は main.py のみ
# ✔ runtime_manager は Runtime 管理のみ
# ✔ token 二重発行防止
# ✔ Scheduler loop 明示化（DISPATCH）
# ✔ pending_entries 起動時完全初期化
# ✔ push_df ready 待ち → summary 初期化を一度だけ実行
# ✔ summary DB 空判定 → BULK / INCR 自動切替
# ✔ ranking snapshot → bar / MA を起動時に必ず再構築
# ✔ Yahoo 補完は core.yahoo_tasks に完全委譲（★FIX）
# ✔ ORM 正本・再起動耐性・安全側設計
# ============================================================

import logging
import threading
import time
import schedule

from core.ws_manager import start_ws
from core.push_manager import start_push_system
from ats import ats_register_loop
from core.position_sync import start_position_sync_loop
from core.scheduler_tasks import register_summary_entry_exit_tasks

from global_state import global_data

# ============================================================
# DB / ORM
# ============================================================
from database.session import Session_summary, Session_ranking
from database.models import (
    StockSummary1Min,
    RankingSnapshot1Min,
)

# ============================================================
# pending
# ============================================================
from trading.entry.pending_manager import clear_all

# ============================================================
# summary rebuild
# ============================================================
from trading.summary.summary_bulk_rebuild import run_bulk_summary_rebuild
from trading.summary.summary_incremental_rebuild import (
    run_incremental_summary_rebuild,
)

# ============================================================
# ranking rebuild
# ============================================================
from trading.ranking.ranking_bar_builder import build_ranking_bar_1min
from trading.ranking.ranking_ma_builder import build_ranking_ma_1min

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _summary_db_empty() -> bool:
    """
    summary DB（1min 正本）が空かどうかを判定
    """
    session = Session_summary()
    try:
        cnt = session.query(StockSummary1Min).count()
        logger.info("📊 summary_1min rows=%d", cnt)
        return cnt == 0
    except Exception:
        logger.exception("❌ summary DB empty check failed")
        return True
    finally:
        session.close()


def _ranking_snapshot_exists() -> bool:
    """
    ranking_snapshot_1min が存在するか
    """
    session = Session_ranking()
    try:
        cnt = session.query(RankingSnapshot1Min).count()
        logger.info("📈 ranking_snapshot_1min rows=%d", cnt)
        return cnt > 0
    except Exception:
        logger.exception("❌ ranking snapshot check failed")
        return False
    finally:
        session.close()


def _wait_for_push_ready(timeout_sec: int = 30) -> bool:
    """
    push_df が最低限 ready になるまで待つ
    """
    start = time.time()
    while time.time() - start < timeout_sec:
        df = getattr(global_data, "push_df", None)
        if df is not None and not df.empty:
            logger.info(
                "✅ push_df ready rows=%d symbols=%d",
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else -1,
            )
            return True
        time.sleep(0.5)

    logger.error("❌ push_df not ready within timeout")
    return False


def _initialize_summary_once():
    """
    起動時 summary を一度だけ初期化
    """
    logger.info("🧠 SUMMARY INIT START")

    if not _wait_for_push_ready():
        logger.error("❌ SUMMARY INIT aborted (push_df not ready)")
        return

    if _summary_db_empty():
        logger.warning("🧱 summary DB empty → BULK rebuild")
        run_bulk_summary_rebuild()
    else:
        logger.info("🔁 summary DB exists → INCREMENTAL rebuild")
        run_incremental_summary_rebuild()

    logger.info("🧠 SUMMARY INIT DONE")


def _initialize_ranking_once():
    """
    起動時 ranking snapshot → bar → MA を再構築
    """
    logger.info("📈 RANKING INIT START")

    if not _ranking_snapshot_exists():
        logger.warning("⚠ ranking snapshot empty → skip ranking init")
        return

    try:
        # snapshot → 疑似1分足
        build_ranking_bar_1min(force=True)
        logger.info("🧱 ranking_bar_1min rebuilt")

        # 疑似1分足 → MA
        build_ranking_ma_1min(
            lookback=120,   # MA75 安定
            force=True,     # 既存 MA 無視
        )
        logger.info("📐 ranking_ma_1min rebuilt")

    except Exception:
        logger.exception("❌ ranking init failed")

    logger.info("📈 RANKING INIT DONE")


# ============================================================
# 各サブシステム起動
# ============================================================

def _start_websocket():
    start_ws()
    logger.info("🌐 WebSocket 起動完了")


def _start_push_system():
    start_push_system()
    logger.info("📦 PUSH System 起動完了")


def _start_ats_register():
    token = global_data.token_value
    threading.Thread(
        target=ats_register_loop,
        args=(token, 10),
        daemon=True,
        name="ATSRegisterLoop",
    ).start()
    logger.info("📡 ATS Register Loop 起動完了")


def _start_position_sync():
    start_position_sync_loop()
    logger.info("📘 Position Sync Loop 起動完了")


def _register_scheduler():
    register_summary_entry_exit_tasks()
    logger.info("⏱️ Scheduler 登録完了")


def _start_scheduler_loop():
    """
    schedule.run_pending() を回し続ける唯一のループ
    """
    logger.info("🕒 Scheduler Loop START")
    while True:
        try:
            schedule.run_pending()
        except Exception:
            logger.exception("❌ Scheduler loop exception (ignored)")
        time.sleep(0.2)


# ============================================================
# ★ メイン：start_runtime
# ============================================================

def start_runtime():

    logger.info("============================================================")
    logger.info("🔥 Kabu AutoTrader System Runtime Starting…")
    logger.info("============================================================")

    # --------------------------------------------------------
    # ★ pending 初期化（安全側）
    # --------------------------------------------------------
    clear_all()
    logger.warning("🧹 pending_entries reset at startup")

    # 1) WebSocket
    _start_websocket()

    # 2) PUSH system
    _start_push_system()

    # 3) ATS Register
    _start_ats_register()

    # 4) Position Sync
    _start_position_sync()

    # --------------------------------------------------------
    # ★ SUMMARY / RANKING 初期化（順序厳守）
    # --------------------------------------------------------
    _initialize_summary_once()
    _initialize_ranking_once()

    # 5) Scheduler 登録（Yahoo含む）
    _register_scheduler()

    # 6) Scheduler Loop
    threading.Thread(
        target=_start_scheduler_loop,
        daemon=True,
        name="SchedulerLoop",
    ).start()

    logger.info("🚀 Runtime System Started 正常稼働中")

    # main thread keep-alive
    while True:
        time.sleep(1)