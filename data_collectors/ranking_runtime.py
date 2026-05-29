# ============================================================
# File   : data_collectors/ranking_runtime.py
# Version: DATA-COLLECTORS-RANKING-RUNTIME-V3-SCHEDULE-COLLECT-AND-SAVE
# ------------------------------------------------------------
# Purpose:
#   - ランキング取得本体を main.py から独立して起動する
#   - ranking DB writer だけでなく、ランキング取得・DB保存tickもこのプロセスで実行する
#
# V3 Fix:
#   ✔ 旧版は start_ranking_db_writer_safe / ensure_ranking_writer_started を呼ぶだけで、
#     実際のランキング取得 job_save_ranking() を schedule 登録していなかった
#   ✔ main.py 側が external collector mode でランキング処理をskipすると、
#     writerだけ起動しても ranking DB に新規保存されない
#   ✔ database collector process 側で job_save_ranking(mode="fast") を毎分 :02 に実行
#   ✔ RANKING_COLLECTOR_ENABLE_FAST_TICK=1 既定ON
#   ✔ RANKING_COLLECTOR_FAST_AT_SECOND=2 既定
#   ✔ 3分ごとに full tick も任意実行可能
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import schedule
import time
from typing import Any

from data_collectors.heartbeat import write_heartbeat
from data_collectors.import_resolver import resolve_callable

logger = logging.getLogger(__name__)


RANKING_START_CANDIDATES = [
    # DB writer 起動候補。取得job登録は本ファイルで別途行う。
    ("core.startup.scheduler_ranking_bootstrap", "start_ranking_db_writer_safe"),
    ("trading.ranking.ranking_db_writer", "ensure_ranking_writer_started"),

    # startup thin wrapper / 旧名候補
    ("core.startup.scheduler_startup", "start_ranking_db_writer_safe"),
    ("core.startup.scheduler_startup", "start_ranking_db_writer"),
    ("core.startup.scheduler_startup", "bootstrap_ranking_db_writer"),

    # ranking writer 旧名候補
    ("trading.ranking.ranking_db_writer", "start_ranking_db_writer"),
    ("trading.ranking.ranking_db_writer", "start"),
    ("trading.ranking.ranking_db_writer", "run_background"),
]

_REGISTERED_TAG_FAST = "ranking_collector_fast_tick"
_REGISTERED_TAG_FULL = "ranking_collector_full_tick"


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if raw in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(float(str(os.getenv(name, str(default))).strip()))
        if min_value is not None:
            v = max(v, min_value)
        if max_value is not None:
            v = min(v, max_value)
        return v
    except Exception:
        return int(default)


def _install_ranking_collector_env() -> None:
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ["AUTOSTOCK_MAIN_DATABASE_PROCESS"] = "1"
    os.environ["AUTOSTOCK_EXTERNAL_DATA_COLLECTORS"] = "1"

    os.environ["AUTOSTOCK_RANKING_COLLECTOR_PROCESS"] = "1"
    os.environ["AUTOSTOCK_RANKING_DB_WRITER"] = "1"
    os.environ.setdefault("AUTOSTOCK_RANKING_SAVE_OWNER", "database")

    # main.py専用DB保存skip設定はランキングcollectorへ持ち込まない。
    os.environ["SUMMARY_SKIP_DB_SAVE_IN_MAIN"] = "0"
    os.environ["SUMMARY_MAIN_ENTRY_ONLY"] = "0"
    os.environ["SUMMARY_DB_WRITER_ROLE"] = "database"

    # writerは即flush寄りにする。大量取得時もbufferに残さない。
    os.environ.setdefault("RANKING_WRITER_BUFFER_SIZE", "1")
    os.environ.setdefault("RANKING_WRITER_FLUSH_INTERVAL_SEC", "1.0")
    os.environ.setdefault("RANKING_WRITER_FLUSH_ON_THRESHOLD", "1")


def start_existing_ranking_writer() -> bool:
    """
    既存コード側の ranking DB writer 起動関数を探して実行する。
    """
    fn = resolve_callable(RANKING_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[RANKING RUNTIME] no existing ranking writer start function resolved")
        return False

    logger.info("[RANKING RUNTIME] call existing ranking writer start function: %s", fn)

    try:
        result = fn()
        logger.info("[RANKING RUNTIME] existing ranking writer start returned: %r", result)
        return True
    except TypeError:
        try:
            result = fn(schedule)
            logger.info("[RANKING RUNTIME] existing ranking writer start returned with schedule: %r", result)
            return True
        except Exception:
            logger.exception("[RANKING RUNTIME] ranking writer start failed with schedule")
            return False
    except Exception:
        logger.exception("[RANKING RUNTIME] ranking writer start failed")
        return False


def _run_ranking_fast_tick() -> None:
    try:
        from trading.ranking.scheduler_core import job_save_ranking

        now = dt.datetime.now().replace(microsecond=0)
        logger.info("[RANKING RUNTIME] fast tick start now=%s", now)
        ret = job_save_ranking(mode="fast", run_full_postprocess=False, save_legacy=False, force=False)
        logger.info("[RANKING RUNTIME] fast tick done ret=%s", ret)
        write_heartbeat("ranking_collector_fast_tick", status="done", detail={"ret": str(ret)[:1000]})
    except Exception as e:
        logger.exception("[RANKING RUNTIME] fast tick failed")
        write_heartbeat("ranking_collector_fast_tick", status="error", detail={"error": str(e)})


def _run_ranking_full_tick() -> None:
    try:
        from trading.ranking.scheduler_core import job_save_ranking

        now = dt.datetime.now().replace(microsecond=0)
        logger.info("[RANKING RUNTIME] full tick start now=%s", now)
        ret = job_save_ranking(mode="full", run_full_postprocess=True, save_legacy=True, force=False)
        logger.info("[RANKING RUNTIME] full tick done ret=%s", ret)
        write_heartbeat("ranking_collector_full_tick", status="done", detail={"ret": str(ret)[:1000]})
    except Exception as e:
        logger.exception("[RANKING RUNTIME] full tick failed")
        write_heartbeat("ranking_collector_full_tick", status="error", detail={"error": str(e)})


def register_ranking_collection_jobs() -> None:
    """
    database collector process 内でランキング取得・保存ジョブを登録する。
    """
    try:
        schedule.clear(_REGISTERED_TAG_FAST)
        schedule.clear(_REGISTERED_TAG_FULL)
    except Exception:
        pass

    enable_fast = _env_bool("RANKING_COLLECTOR_ENABLE_FAST_TICK", True)
    enable_full = _env_bool("RANKING_COLLECTOR_ENABLE_FULL_TICK", False)
    at_second = _env_int("RANKING_COLLECTOR_FAST_AT_SECOND", 2, min_value=0, max_value=59)
    full_every_min = _env_int("RANKING_COLLECTOR_FULL_EVERY_MINUTES", 3, min_value=1, max_value=60)

    if enable_fast:
        job = schedule.every().minute.at(f":{at_second:02d}").do(_run_ranking_fast_tick)
        try:
            job.tag(_REGISTERED_TAG_FAST)
        except Exception:
            pass
        logger.info("[RANKING RUNTIME] registered fast ranking collect/save tick every minute at :%02d", at_second)

    if enable_full:
        job = schedule.every(full_every_min).minutes.do(_run_ranking_full_tick)
        try:
            job.tag(_REGISTERED_TAG_FULL)
        except Exception:
            pass
        logger.info("[RANKING RUNTIME] registered full ranking collect/save tick every %d minutes", full_every_min)

    # 起動直後にも1回実行して、DBが空の時間を減らす。
    if enable_fast and _env_bool("RANKING_COLLECTOR_RUN_ON_START", True):
        logger.info("[RANKING RUNTIME] run first fast tick on start")
        _run_ranking_fast_tick()


def run_forever() -> int:
    _install_ranking_collector_env()

    logger.info("[RANKING RUNTIME] START")
    logger.info(
        "[RANKING RUNTIME] env writer=%s owner=%s fast=%s full=%s flush_on_threshold=%s buffer=%s",
        os.getenv("AUTOSTOCK_RANKING_DB_WRITER"),
        os.getenv("AUTOSTOCK_RANKING_SAVE_OWNER"),
        os.getenv("RANKING_COLLECTOR_ENABLE_FAST_TICK"),
        os.getenv("RANKING_COLLECTOR_ENABLE_FULL_TICK"),
        os.getenv("RANKING_WRITER_FLUSH_ON_THRESHOLD"),
        os.getenv("RANKING_WRITER_BUFFER_SIZE"),
    )

    ok = start_existing_ranking_writer()
    if not ok:
        logger.error("[RANKING RUNTIME] abort because ranking writer could not start")
        return 1

    register_ranking_collection_jobs()

    last_hb = 0.0

    while True:
        try:
            schedule.run_pending()
        except Exception:
            logger.exception("[RANKING RUNTIME] schedule.run_pending failed")

        now = time.time()
        if now - last_hb >= 30:
            write_heartbeat("ranking_collector", status="alive")
            logger.info("[RANKING RUNTIME] heartbeat alive")
            last_hb = now

        time.sleep(0.5)


__all__ = [
    "run_forever",
    "start_existing_ranking_writer",
    "register_ranking_collection_jobs",
]
