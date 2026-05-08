# ============================================================
# File   : scripts/summary_database_runner.py
# Version: SUMMARY-DATABASE-RUNNER-V1
# ------------------------------------------------------------
# Purpose:
#   - main_database.py 側で定時サマリー計算・DB保存を担当する子プロセス
#   - main.py 側でも計算は継続するが、DB保存 owner は database 側へ寄せる
#   - 1分ごとに run_time_locked_summary_jobs() を実行する
#
# Policy:
#   - display=False
#   - run_entry=False
#   - DB save enabled only in database/data collector process
#   - PUSH summary は毎分/3分/5分周期で計算・保存
#   - RANKING summary は ENABLE_RANKING_SUMMARY_TICK=1 の場合だけ実行
#
# Environment:
#   AUTOSTOCK_DATA_COLLECTORS_PROCESS=1
#   AUTOSTOCK_SUMMARY_DB_WRITER=1
#   AUTOSTOCK_SUMMARY_SAVE_OWNER=database
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    os.chdir(str(PROJECT_ROOT))
except Exception:
    pass

from data_collectors.logging_setup import setup_logging
from scheduler_jobs.summary.time_locked_runner import run_time_locked_summary_jobs


_STOP = False


def _env_true(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if raw in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    except Exception:
        pass
    return bool(default)


def _handle_signal(signum, frame) -> None:
    global _STOP
    _STOP = True


def _floor_minute(x: dt.datetime | None = None) -> dt.datetime:
    return (x or dt.datetime.now()).replace(second=0, microsecond=0)


def _sleep_until_next_minute(logger: logging.Logger) -> None:
    try:
        now = dt.datetime.now()
        next_min = (now + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
        sleep_sec = max(0.2, (next_min - now).total_seconds())
    except Exception:
        sleep_sec = 1.0

    logger.debug("[SUMMARY DB RUNNER] sleep %.3fs", sleep_sec)
    time.sleep(sleep_sec)


def _install_database_summary_env() -> None:
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ["AUTOSTOCK_SUMMARY_DB_WRITER"] = "1"
    os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
    os.environ.setdefault("AUTOSTOCK_SUMMARY_SAVE_MODE", "save")

    # main_database 側ではAI/entryは実行しない。計算とDB保存のみ。
    os.environ.setdefault("ENABLE_SUMMARY_ENTRY_TICK", "0")


def main() -> int:
    _install_database_summary_env()

    logger = setup_logging("summary_database_runner")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=" * 80)
    logger.info("[SUMMARY DB RUNNER] START")
    logger.info("[SUMMARY DB RUNNER] PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("[SUMMARY DB RUNNER] cwd=%s", os.getcwd())
    logger.info("[SUMMARY DB RUNNER] AUTOSTOCK_SUMMARY_SAVE_OWNER=%s", os.getenv("AUTOSTOCK_SUMMARY_SAVE_OWNER"))
    logger.info("[SUMMARY DB RUNNER] AUTOSTOCK_SUMMARY_SAVE_MODE=%s", os.getenv("AUTOSTOCK_SUMMARY_SAVE_MODE"))
    logger.info("[SUMMARY DB RUNNER] ENABLE_RANKING_SUMMARY_TICK=%s", os.getenv("ENABLE_RANKING_SUMMARY_TICK"))
    logger.info("=" * 80)

    last_run_minute: dt.datetime | None = None

    while not _STOP:
        now = _floor_minute()

        if last_run_minute == now:
            _sleep_until_next_minute(logger)
            continue

        last_run_minute = now

        try:
            ranking_enabled = _env_true("ENABLE_RANKING_SUMMARY_TICK", default=False)

            logger.info(
                "[SUMMARY DB RUNNER] tick start now=%s run_push=True run_ranking=%s display=False run_entry=False",
                now,
                ranking_enabled,
            )

            t0 = time.perf_counter()

            result = run_time_locked_summary_jobs(
                now=now,
                run_push=True,
                run_ranking=ranking_enabled,
                display=False,
                run_entry=False,
            )

            push_rows = {
                int(k): len(v) if hasattr(v, "__len__") else 0
                for k, v in (result.get("push", {}) or {}).items()
            } if isinstance(result, dict) else {}

            ranking_rows = {
                int(k): len(v) if hasattr(v, "__len__") else 0
                for k, v in (result.get("ranking", {}) or {}).items()
            } if isinstance(result, dict) else {}

            logger.info(
                "[SUMMARY DB RUNNER] tick done now=%s elapsed=%.3fs push_rows=%s ranking_rows=%s",
                now,
                time.perf_counter() - t0,
                push_rows,
                ranking_rows,
            )

        except Exception:
            logger.exception("[SUMMARY DB RUNNER] tick failed now=%s", now)

        _sleep_until_next_minute(logger)

    logger.warning("[SUMMARY DB RUNNER] STOPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
