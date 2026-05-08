# ============================================================
# File   : scripts/summary_database_runner.py
# Version: SUMMARY-DATABASE-RUNNER-V2-MULTIDAY-MA75-WARMUP
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
# V2 Fix:
#   ✔ main_database.py 側でも起動直後に複数日summary tailを読み込む
#   ✔ 5分足75MA用に前日/前々日DBを含めたtailをglobal cacheへ投入
#   ✔ summary_database_runner の初回tickからMA75欠損を減らす
#   ✔ warmup失敗でもrunnerは継続
#
# Environment:
#   AUTOSTOCK_DATA_COLLECTORS_PROCESS=1
#   AUTOSTOCK_SUMMARY_DB_WRITER=1
#   AUTOSTOCK_SUMMARY_SAVE_OWNER=database
#   PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS=3
#   PUSH_INCREMENTAL_MA75_TAIL_ROWS=120
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

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
    os.environ["AUTOSTOCK_MAIN_DATABASE_PROCESS"] = "1"
    os.environ["AUTOSTOCK_SUMMARY_DB_WRITER"] = "1"
    os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
    os.environ.setdefault("AUTOSTOCK_SUMMARY_SAVE_MODE", "save")

    # main_database 側ではAI/entryは実行しない。計算とDB保存のみ。
    os.environ.setdefault("ENABLE_SUMMARY_ENTRY_TICK", "0")

    # 5分足75MAは当日DBだけでは不足するため、前日/前々日を含めて読む。
    os.environ.setdefault("PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS", "3")
    os.environ.setdefault("PUSH_INCREMENTAL_MA75_TAIL_ROWS", "120")


def _warmup_multiday_ma75_cache(logger: logging.Logger) -> Any:
    """
    main_database.py 側の summary保存プロセスでも、定時サマリー計算前に
    複数日summary tailをglobal cacheへ投入する。

    理由:
      - 5分足75MAは当日だけでは75本に不足することがある
      - main.pyだけでなく、DB保存ownerであるsummary_database_runner側も
        同じ履歴を持って計算する必要がある
    """
    try:
        logger.info("[SUMMARY DB RUNNER] multiday MA75 warmup start")

        from core.startup.startup_push_incremental_ma75 import build_push_incremental_ma75_on_startup

        result = build_push_incremental_ma75_on_startup(
            intervals=(1, 3, 5),
            update_global_cache=True,
        )

        logger.info(
            "[SUMMARY DB RUNNER] multiday MA75 warmup done ok=%s msg=%s "
            "summary_dbs=%s push_db=%s loaded_summary_rows=%s cache_rows=%s ma75_nonnull=%s latest=%s",
            bool(getattr(result, "ok", False)),
            getattr(result, "message", ""),
            getattr(result, "summary_dbs", None),
            getattr(result, "push_db", None),
            getattr(result, "loaded_summary_rows", None),
            getattr(result, "cache_rows", None),
            getattr(result, "ma75_nonnull", None),
            getattr(result, "latest", None),
        )
        return result

    except Exception:
        logger.exception("[SUMMARY DB RUNNER] multiday MA75 warmup failed; continue runner")
        return None


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
    logger.info("[SUMMARY DB RUNNER] PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS=%s", os.getenv("PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS"))
    logger.info("[SUMMARY DB RUNNER] PUSH_INCREMENTAL_MA75_TAIL_ROWS=%s", os.getenv("PUSH_INCREMENTAL_MA75_TAIL_ROWS"))
    logger.info("=" * 80)

    # 初回tick前に、前日/前々日を含むsummary tailをcacheへ投入する。
    _warmup_multiday_ma75_cache(logger)

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
