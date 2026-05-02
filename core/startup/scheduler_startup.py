# ============================================================
# File   : core/startup/scheduler_startup.py
# Version: FINAL-PRODUCTION-REV23.1-SCHEDULER-STARTUP-RANKING-WRITER
# ------------------------------------------------------------
# 【概要】
#   scheduler 登録・schedule.run_pending loop・summary tick once debug を担当
#
# 【機能】
#   ✔ scheduler bootstrap early
#   ✔ schedule.run_pending loop start
#   ✔ summary tick once debug
#   ✔ scheduler fallback
#   ✔ schedule loop fallback
#   ✔ schedule.jobs snapshot
#   ✔ ranking DB writer 明示起動
#      - trading/ranking/ranking_db_writer.py の存在確認
#      - ensure_ranking_writer_started() を起動時に呼ぶ
#      - 市場時間外でも [RANKING DB WRITER] connected を確認可能
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import schedule

from global_state import global_data
from core.startup.scheduler_bootstrap import register_scheduler_safe
from core.startup.schedule_loop import (
    start_schedule_run_pending_loop_safe,
    get_schedule_loop_status,
)
from core.startup.startup_config import resolve_attr

logger = logging.getLogger(__name__)

VERSION = "FINAL-PRODUCTION-REV23.1-SCHEDULER-STARTUP-RANKING-WRITER"


# ============================================================
# snapshot helpers
# ============================================================

def safe_schedule_snapshot(limit: int = 50) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []

    try:
        jobs = list(getattr(schedule, "jobs", []) or [])
    except Exception:
        return snapshot

    for j in jobs[: int(limit)]:
        try:
            snapshot.append(
                {
                    "job": repr(j),
                    "tags": sorted([str(x) for x in (getattr(j, "tags", set()) or set())]),
                    "next_run": str(getattr(j, "next_run", None)),
                    "last_run": str(getattr(j, "last_run", None)),
                    "interval": str(getattr(j, "interval", None)),
                    "unit": str(getattr(j, "unit", None)),
                }
            )
        except Exception:
            try:
                snapshot.append({"job": repr(j)})
            except Exception:
                snapshot.append({"job": "<unrepresentable>"})

    return snapshot


def log_scheduler_snapshot(context: str) -> None:
    try:
        jobs = list(getattr(schedule, "jobs", []) or [])
    except Exception:
        jobs = []

    logger.info(
        "[scheduler_startup][SCHEDULER SNAPSHOT] context=%s jobs=%s snapshot=%s",
        context,
        len(jobs),
        safe_schedule_snapshot(limit=50),
    )


# ============================================================
# ranking DB writer bootstrap
# ============================================================

def start_ranking_db_writer_safe() -> bool:
    """
    PUSH DB WRITER と同じ方式にしたランキングDB writerを起動する。

    目的:
      - 市場時間外でも ranking writer の import / 起動確認ログを出す
      - [RANKING DB WRITER] connected / writer thread started を確認可能にする
      - scheduler_core からの lazy start だけに依存しない

    注意:
      - ここではランキング取得・保存は行わない
      - writer thread と sqlite3 接続を準備するだけ
      - 失敗してもシステム起動全体は止めない
    """
    logger.info("[startup.scheduler_startup] ranking db writer bootstrap start")

    try:
        from trading.ranking.ranking_db_writer import ensure_ranking_writer_started

        writer = ensure_ranking_writer_started()

        try:
            global_data.ranking_db_writer_bootstrap_done = True
            global_data.ranking_db_writer_bootstrap_failed = False
            global_data.ranking_db_writer_bootstrap_at = dt.datetime.now()
            global_data.ranking_db_writer_instance_type = type(writer).__name__
        except Exception:
            pass

        logger.info(
            "[startup.scheduler_startup] ranking db writer started writer=%s",
            type(writer).__name__,
        )
        return True

    except Exception as e:
        try:
            global_data.ranking_db_writer_bootstrap_done = False
            global_data.ranking_db_writer_bootstrap_failed = True
            global_data.ranking_db_writer_bootstrap_error = str(e)
            global_data.ranking_db_writer_bootstrap_at = dt.datetime.now()
        except Exception:
            pass

        logger.exception("[startup.scheduler_startup] ranking db writer start failed")
        return False


# ============================================================
# scheduler register
# ============================================================

def register_scheduler_early_safe() -> bool:
    """
    startup_summary_restore より前に scheduler を登録する。
    """
    logger.info("🕒 scheduler bootstrap early start before startup_summary_restore")

    log_scheduler_snapshot("before early scheduler bootstrap")

    ok = False

    try:
        register_scheduler_safe()
        ok = True
    except Exception:
        logger.exception("❌ scheduler bootstrap early failed")
        ok = False

    try:
        global_data.scheduler_bootstrap_registered = bool(ok)
        global_data.scheduler_bootstrap_registered_at = dt.datetime.now() if ok else None
        global_data.scheduler_bootstrap_failed = not bool(ok)
        global_data.scheduler_bootstrap_result = {
            "ok": bool(ok),
            "phase": "before_startup_summary_restore",
        }
    except Exception:
        pass

    log_scheduler_snapshot("after early scheduler bootstrap")

    if ok:
        logger.info("✅ scheduler bootstrap early complete")
    else:
        logger.warning("⚠ scheduler bootstrap early completed ok=False")

    return bool(ok)


def register_scheduler_fallback_safe() -> bool:
    """
    後段の保険登録。
    早期登録済みなら skip。
    """
    try:
        already = bool(getattr(global_data, "scheduler_bootstrap_registered", False))
    except Exception:
        already = False

    if already:
        logger.info("🕒 scheduler bootstrap fallback skipped: already registered")
        log_scheduler_snapshot("scheduler fallback skipped")
        return True

    logger.info("🕒 scheduler bootstrap fallback start")

    ok = False
    try:
        register_scheduler_safe()
        ok = True
    except Exception:
        logger.exception("❌ scheduler bootstrap fallback failed")
        ok = False

    try:
        global_data.scheduler_bootstrap_registered = bool(ok)
        global_data.scheduler_bootstrap_registered_at = dt.datetime.now() if ok else None
        global_data.scheduler_bootstrap_failed = not bool(ok)
    except Exception:
        pass

    log_scheduler_snapshot("after scheduler fallback")

    return bool(ok)


# ============================================================
# schedule loop
# ============================================================

def start_schedule_loop_early_safe() -> bool:
    """
    schedule.run_pending loop を起動する。
    """
    logger.info("🕒 schedule run_pending loop start after scheduler bootstrap")

    try:
        ok = start_schedule_run_pending_loop_safe(
            interval_seconds=0.5,
            heartbeat_seconds=30.0,
            snapshot_limit=30,
        )
    except Exception:
        logger.exception("❌ schedule run_pending loop start failed")
        ok = False

    logger.info(
        "🕒 schedule run_pending loop result ok=%s status=%s",
        ok,
        get_schedule_loop_status(),
    )

    return bool(ok)


def ensure_schedule_loop_running_safe() -> bool:
    """
    万一 early loop が起動できていなければ再試行。
    """
    try:
        loop_status = get_schedule_loop_status()
        if not bool(loop_status.get("running")):
            logger.warning("🕒 schedule loop fallback start because not running status=%s", loop_status)
            return start_schedule_loop_early_safe()

        logger.info("🕒 schedule loop fallback skipped: already running status=%s", loop_status)
        return True

    except Exception:
        logger.exception("❌ schedule loop fallback check failed")
        return False


# ============================================================
# summary tick once debug
# ============================================================

def run_summary_tick_once_debug_safe() -> bool:
    """
    scheduler loop を待たずに summary tick を1回だけ手動実行する。
    起動直後の表示経路診断用。
    """
    logger.info("🧪 summary tick once debug start after scheduler bootstrap")

    try:
        fn = resolve_attr("scheduler_jobs.summary.scheduler", "run_summary_tick_once")

        if not callable(fn):
            logger.warning(
                "🧪 summary tick once debug skipped: "
                "scheduler_jobs.summary.scheduler.run_summary_tick_once not found"
            )
            try:
                global_data.summary_tick_once_debug_done = False
                global_data.summary_tick_once_debug_failed = True
                global_data.summary_tick_once_debug_result = "function_not_found"
            except Exception:
                pass
            return False

        ret = fn()

        try:
            global_data.summary_tick_once_debug_done = True
            global_data.summary_tick_once_debug_failed = False
            global_data.summary_tick_once_debug_result = ret
            global_data.summary_tick_once_debug_at = dt.datetime.now()
        except Exception:
            pass

        logger.info("✅ summary tick once debug done ret=%s", ret)
        return True

    except Exception:
        try:
            global_data.summary_tick_once_debug_done = False
            global_data.summary_tick_once_debug_failed = True
            global_data.summary_tick_once_debug_result = "exception"
        except Exception:
            pass

        logger.exception("❌ summary tick once debug failed")
        return False


def start_scheduler_stack_before_restore() -> None:
    """
    scheduler登録、ranking writer起動、run_pending loop、tick once debug をまとめて実行。

    順序:
      1. scheduler登録
      2. ranking DB writer明示起動
      3. schedule.run_pending loop起動
      4. summary tick once debug

    ranking writer をここで起動する理由:
      - 市場時間外は ranking_save_tick が closed-day reuse で0件終了し、
        writer が lazy start されないことがある
      - 起動時に [RANKING DB WRITER] connected を確認するため
    """
    register_scheduler_early_safe()
    start_ranking_db_writer_safe()
    start_schedule_loop_early_safe()
    run_summary_tick_once_debug_safe()


__all__ = [
    "VERSION",
    "safe_schedule_snapshot",
    "log_scheduler_snapshot",
    "start_ranking_db_writer_safe",
    "register_scheduler_early_safe",
    "register_scheduler_fallback_safe",
    "start_schedule_loop_early_safe",
    "ensure_schedule_loop_running_safe",
    "run_summary_tick_once_debug_safe",
    "start_scheduler_stack_before_restore",
]