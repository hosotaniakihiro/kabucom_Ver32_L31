# ============================================================
# File   : scripts/summary_database_runner.py
# Version: SUMMARY-DATABASE-RUNNER-V11-PUSH-DB-SOURCE-FALLBACK
# ------------------------------------------------------------
# Purpose:
#   - main_database.py 側で定時サマリー計算・DB保存を担当する子プロセス
#   - DB保存 owner は database 側へ寄せる
#   - PUSH由来サマリーを毎分DB保存する
#
# V11:
#   - summary_database は push_receiver と別プロセスなので、global_data.push_df を信用しすぎない。
#   - push_summary_engine に PUSH DB 読みフォールバックを入れ、push_df_empty による古いsummary再利用を防ぐ。
#
# V10:
#   - 3m/5m を毎分強制計算しない。境界分だけ計算して遅延を防ぐ。
#   - slow tick 後の「次tickスキップ」を既定OFFにし、遅延復帰を早める。
#   - spool flush は起動時/定期だけに抑え、after_tick連続失敗で重くしない。
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


def _install_database_summary_env() -> None:
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ["AUTOSTOCK_MAIN_DATABASE_PROCESS"] = "1"
    os.environ["AUTOSTOCK_SUMMARY_DB_WRITER"] = "1"
    os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
    os.environ["AUTOSTOCK_SUMMARY_SAVE_MODE"] = "save"

    # main_database.py は保存専用寄せ。表示/通知は main.py 側に寄せる。
    os.environ["SUMMARY_DATABASE_RUNNER_DISPLAY"] = "0"
    os.environ["SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY"] = "0"
    os.environ.setdefault("SUMMARY_SAVE_SPOOL_FLUSH", "1")

    # summary_database は push_receiver と別プロセスなので、PUSHメモリが空/古い場合は
    # pushYYYYMMDD.db から最新PUSHを読む (trading/summary/engine/push_summary_engine.py
    # _resolve_push_source_df 本体に統合済み。この env はそちらのスコープ切り替え)。
    os.environ.setdefault("PUSH_SUMMARY_DB_SOURCE_FALLBACK", "1")
    os.environ.setdefault("PUSH_SUMMARY_DB_LOOKBACK_MIN", "20")
    os.environ.setdefault("PUSH_SUMMARY_DB_MAX_ROWS", "30000")
    os.environ.setdefault("PUSH_SUMMARY_DB_SOURCE_MAX_MEM_LAG_SEC", "30")
    os.environ.setdefault("PUSH_SUMMARY_DB_BUSY_TIMEOUT_MS", "5000")

    # scheduler_jobs/summary/safe_io.py の空サマリーDiscord通知は、main.py の通常tickとの
    # 重複通知を避けるため既定オフ。summary_database_runner.py だけこのプロセススコープを開ける。
    os.environ.setdefault("SUMMARY_DISCORD_EMPTY_FALLBACK_SCOPE_ENABLED", "1")

    # V10:
    # 3m/5mを毎分計算すると、NAS SQLite + 404銘柄級で1tickが10分超に肥大化し、
    # PUSH summary が stale drop される。既定は境界分だけ実行に戻す。
    # どうしても毎分3m/5mを計算したい場合だけ settings.ini/env で 1 にする。
    raw_mtf_every_minute = str(os.getenv("SUMMARY_DATABASE_MTF_EVERY_MINUTE", "0")).strip().lower()
    mtf_every_minute = "1" if raw_mtf_every_minute in {"1", "true", "yes", "on", "enable", "enabled"} else "0"
    os.environ["SUMMARY_DATABASE_MTF_EVERY_MINUTE"] = mtf_every_minute
    os.environ["SUMMARY_PUSH_DISPLAY_ALL_INTERVALS"] = mtf_every_minute
    os.environ["SUMMARY_PARALLEL_FORCE_1_3_5"] = "0"
    os.environ["SUMMARY_PARALLEL_INTERVAL_WORKERS"] = "1"
    os.environ["SUMMARY_PUSH_BG_INTERVAL_WORKERS"] = "1"

    # spool flush は毎tick前後に無条件実行しない。
    os.environ.setdefault("SUMMARY_SAVE_SPOOL_FLUSH_MIN_INTERVAL_SEC", "300")
    os.environ.setdefault("SUMMARY_SAVE_SPOOL_FLUSH_MAX_FILES", "3")
    os.environ.setdefault("SUMMARY_SAVE_SPOOL_FLUSH_AFTER_TICK", "0")

    # slow tick 後に次tickを休ませると、すでに遅れているサマリーがさらに古くなる。
    os.environ.setdefault("SUMMARY_DATABASE_SLOW_TICK_SEC", "45")
    os.environ.setdefault("SUMMARY_DATABASE_SKIP_NEXT_ON_SLOW_TICK", "0")

    os.environ["SUMMARY_SKIP_DB_SAVE_IN_MAIN"] = "0"
    os.environ["SUMMARY_MAIN_ENTRY_ONLY"] = "0"
    os.environ["SUMMARY_DB_WRITER_ROLE"] = "database"

    os.environ["ENABLE_SUMMARY_ENTRY_TICK"] = "0"
    os.environ["ENABLE_RANKING_SUMMARY_TICK"] = "0"
    os.environ.setdefault("PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS", "1")
    os.environ.setdefault("PUSH_INCREMENTAL_MA75_TAIL_ROWS", "90")


_install_database_summary_env()

from data_collectors.logging_setup import setup_logging
from scheduler_jobs.summary.time_locked_runner import run_time_locked_summary_jobs


_STOP = False
_LAST_SPOOL_FLUSH_TS = 0.0


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


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, str(default))).strip()))
    except Exception:
        return int(default)


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


def _warmup_multiday_ma75_cache(logger: logging.Logger) -> Any:
    if not _env_true("SUMMARY_DATABASE_MA75_WARMUP", default=True):
        logger.warning("[SUMMARY DB RUNNER] multiday MA75 warmup skipped by SUMMARY_DATABASE_MA75_WARMUP=0")
        return None
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


def _flush_summary_save_spool(logger: logging.Logger, *, reason: str, force: bool = False) -> dict:
    global _LAST_SPOOL_FLUSH_TS

    if not _env_true("SUMMARY_SAVE_SPOOL_FLUSH", default=True):
        return {"disabled": True}

    if reason == "after_tick" and not _env_true("SUMMARY_SAVE_SPOOL_FLUSH_AFTER_TICK", default=False):
        return {"skipped": True, "reason": "after_tick_disabled"}

    now_ts = time.monotonic()
    min_interval = max(0.0, _env_float("SUMMARY_SAVE_SPOOL_FLUSH_MIN_INTERVAL_SEC", 300.0))
    if not force and _LAST_SPOOL_FLUSH_TS and now_ts - _LAST_SPOOL_FLUSH_TS < min_interval:
        return {"skipped": True, "reason": "min_interval", "age_sec": round(now_ts - _LAST_SPOOL_FLUSH_TS, 3)}

    try:
        max_files = max(1, _env_int("SUMMARY_SAVE_SPOOL_FLUSH_MAX_FILES", 3))
        from trading.summary.persistence.summary_save_spool import flush_summary_spool
        result = flush_summary_spool(max_files=max_files)
        _LAST_SPOOL_FLUSH_TS = now_ts
        if result.get("files", 0):
            logger.warning("[SUMMARY DB RUNNER] spool flush reason=%s max_files=%s result=%s", reason, max_files, result)
        return result
    except Exception:
        logger.exception("[SUMMARY DB RUNNER] spool flush failed reason=%s", reason)
        return {"error": True}


def main() -> int:
    _install_database_summary_env()

    logger = setup_logging("summary_database_runner")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=" * 80)
    logger.info("[SUMMARY DB RUNNER] START")
    logger.info("[SUMMARY DB RUNNER] PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("[SUMMARY DB RUNNER] cwd=%s", os.getcwd())
    logger.info("[SUMMARY DB RUNNER] AUTOSTOCK_DATA_COLLECTORS_PROCESS=%s", os.getenv("AUTOSTOCK_DATA_COLLECTORS_PROCESS"))
    logger.info("[SUMMARY DB RUNNER] AUTOSTOCK_MAIN_DATABASE_PROCESS=%s", os.getenv("AUTOSTOCK_MAIN_DATABASE_PROCESS"))
    logger.info("[SUMMARY DB RUNNER] AUTOSTOCK_SUMMARY_DB_WRITER=%s", os.getenv("AUTOSTOCK_SUMMARY_DB_WRITER"))
    logger.info("[SUMMARY DB RUNNER] AUTOSTOCK_SUMMARY_SAVE_OWNER=%s", os.getenv("AUTOSTOCK_SUMMARY_SAVE_OWNER"))
    logger.info("[SUMMARY DB RUNNER] AUTOSTOCK_SUMMARY_SAVE_MODE=%s", os.getenv("AUTOSTOCK_SUMMARY_SAVE_MODE"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_DATABASE_RUNNER_DISPLAY=%s", os.getenv("SUMMARY_DATABASE_RUNNER_DISPLAY"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_DATABASE_MTF_EVERY_MINUTE=%s", os.getenv("SUMMARY_DATABASE_MTF_EVERY_MINUTE"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_PUSH_DISPLAY_ALL_INTERVALS=%s", os.getenv("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_PARALLEL_FORCE_1_3_5=%s workers=%s bg_workers=%s", os.getenv("SUMMARY_PARALLEL_FORCE_1_3_5"), os.getenv("SUMMARY_PARALLEL_INTERVAL_WORKERS"), os.getenv("SUMMARY_PUSH_BG_INTERVAL_WORKERS"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_SAVE_SPOOL_FLUSH=%s min_interval=%s max_files=%s after_tick=%s", os.getenv("SUMMARY_SAVE_SPOOL_FLUSH"), os.getenv("SUMMARY_SAVE_SPOOL_FLUSH_MIN_INTERVAL_SEC"), os.getenv("SUMMARY_SAVE_SPOOL_FLUSH_MAX_FILES"), os.getenv("SUMMARY_SAVE_SPOOL_FLUSH_AFTER_TICK"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY=%s scope_enabled=%s", os.getenv("SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY"), os.getenv("SUMMARY_DISCORD_EMPTY_FALLBACK_SCOPE_ENABLED"))
    logger.info("[SUMMARY DB RUNNER] PUSH_SUMMARY_DB_SOURCE_FALLBACK=%s lookback_min=%s max_rows=%s max_mem_lag_sec=%s", os.getenv("PUSH_SUMMARY_DB_SOURCE_FALLBACK"), os.getenv("PUSH_SUMMARY_DB_LOOKBACK_MIN"), os.getenv("PUSH_SUMMARY_DB_MAX_ROWS"), os.getenv("PUSH_SUMMARY_DB_SOURCE_MAX_MEM_LAG_SEC"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_SKIP_DB_SAVE_IN_MAIN=%s", os.getenv("SUMMARY_SKIP_DB_SAVE_IN_MAIN"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_MAIN_ENTRY_ONLY=%s", os.getenv("SUMMARY_MAIN_ENTRY_ONLY"))
    logger.info("[SUMMARY DB RUNNER] SUMMARY_DB_WRITER_ROLE=%s", os.getenv("SUMMARY_DB_WRITER_ROLE"))
    logger.info("[SUMMARY DB RUNNER] ENABLE_RANKING_SUMMARY_TICK=%s", os.getenv("ENABLE_RANKING_SUMMARY_TICK"))
    logger.info("[SUMMARY DB RUNNER] PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS=%s", os.getenv("PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS"))
    logger.info("[SUMMARY DB RUNNER] PUSH_INCREMENTAL_MA75_TAIL_ROWS=%s", os.getenv("PUSH_INCREMENTAL_MA75_TAIL_ROWS"))
    logger.info("[SUMMARY DB RUNNER] slow_tick_sec=%s skip_next_on_slow=%s", os.getenv("SUMMARY_DATABASE_SLOW_TICK_SEC"), os.getenv("SUMMARY_DATABASE_SKIP_NEXT_ON_SLOW_TICK"))
    logger.info("=" * 80)

    _warmup_multiday_ma75_cache(logger)
    _flush_summary_save_spool(logger, reason="startup", force=True)

    last_run_minute: dt.datetime | None = None
    skip_next_tick = False

    while not _STOP:
        now = _floor_minute()

        if last_run_minute == now:
            _sleep_until_next_minute(logger)
            continue

        last_run_minute = now

        if skip_next_tick:
            skip_next_tick = False
            logger.warning("[SUMMARY DB RUNNER] tick skipped once after slow previous tick now=%s", now)
            _sleep_until_next_minute(logger)
            continue

        try:
            _install_database_summary_env()
            ranking_enabled = _env_true("ENABLE_RANKING_SUMMARY_TICK", default=False)
            display_enabled = _env_true("SUMMARY_DATABASE_RUNNER_DISPLAY", default=False)

            logger.info(
                "[SUMMARY DB RUNNER] tick start now=%s run_push=True run_ranking=%s display=%s run_entry=False save_owner=%s save_mode=%s skip_main=%s role=%s empty_notify=%s spool_flush=%s mtf_every_minute=%s push_all_intervals=%s force_1_3_5=%s workers=%s",
                now,
                ranking_enabled,
                display_enabled,
                os.getenv("AUTOSTOCK_SUMMARY_SAVE_OWNER"),
                os.getenv("AUTOSTOCK_SUMMARY_SAVE_MODE"),
                os.getenv("SUMMARY_SKIP_DB_SAVE_IN_MAIN"),
                os.getenv("SUMMARY_DB_WRITER_ROLE"),
                os.getenv("SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY"),
                os.getenv("SUMMARY_SAVE_SPOOL_FLUSH"),
                os.getenv("SUMMARY_DATABASE_MTF_EVERY_MINUTE"),
                os.getenv("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS"),
                os.getenv("SUMMARY_PARALLEL_FORCE_1_3_5"),
                os.getenv("SUMMARY_PARALLEL_INTERVAL_WORKERS"),
            )

            _flush_summary_save_spool(logger, reason="before_tick")

            t0 = time.perf_counter()

            result = run_time_locked_summary_jobs(
                now=now,
                run_push=True,
                run_ranking=ranking_enabled,
                display=display_enabled,
                run_entry=False,
            )

            _flush_summary_save_spool(logger, reason="after_tick")

            elapsed = time.perf_counter() - t0
            slow_tick_sec = max(1.0, _env_float("SUMMARY_DATABASE_SLOW_TICK_SEC", 45.0))
            if elapsed >= slow_tick_sec:
                if _env_true("SUMMARY_DATABASE_SKIP_NEXT_ON_SLOW_TICK", default=False):
                    skip_next_tick = True
                    logger.warning(
                        "[SUMMARY DB RUNNER] slow tick detected elapsed=%.3fs threshold=%.3fs -> skip next tick once",
                        elapsed,
                        slow_tick_sec,
                    )
                else:
                    logger.warning(
                        "[SUMMARY DB RUNNER] slow tick detected elapsed=%.3fs threshold=%.3fs -> continue next tick",
                        elapsed,
                        slow_tick_sec,
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
                "[SUMMARY DB RUNNER] tick done now=%s elapsed=%.3fs push_rows=%s ranking_rows=%s display=%s",
                now,
                elapsed,
                push_rows,
                ranking_rows,
                display_enabled,
            )

        except Exception:
            logger.exception("[SUMMARY DB RUNNER] tick failed now=%s", now)

        _sleep_until_next_minute(logger)

    logger.warning("[SUMMARY DB RUNNER] STOPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
