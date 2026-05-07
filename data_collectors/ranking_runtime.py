# ============================================================
# File   : data_collectors/ranking_runtime.py
# Version: DATA-COLLECTORS-RANKING-RUNTIME-V1
# ------------------------------------------------------------
# Purpose:
#   - ランキング取得本体を main.py から独立して起動する
#   - 既存プロジェクトの起動関数を候補順に解決して呼び出す
# ============================================================

from __future__ import annotations

import logging
import schedule
import time

from data_collectors.heartbeat import write_heartbeat
from data_collectors.import_resolver import resolve_callable

logger = logging.getLogger(__name__)


RANKING_START_CANDIDATES = [
    # 既存候補。プロジェクト側の実名に合わせて必要なら追加してください。
    ("core.startup.scheduler_startup", "start_ranking_db_writer"),
    ("core.startup.scheduler_startup", "bootstrap_ranking_db_writer"),
    ("trading.ranking.ranking_db_writer", "start_ranking_db_writer"),
    ("trading.ranking.ranking_db_writer", "start"),
    ("scheduler_jobs.ranking_save.runner", "register_ranking_save_tasks"),
    ("scheduler_jobs.ranking.runner", "register_ranking_tasks"),
    ("scheduler_jobs.ranking_save", "register_ranking_save_tasks"),
]


def start_existing_ranking_collector() -> bool:
    """
    既存コード側のランキング取得・保存起動関数を探して実行する。

    戻り値:
        True  = 何らかの起動関数を呼べた
        False = 候補が見つからなかった
    """
    fn = resolve_callable(RANKING_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[RANKING RUNTIME] no existing ranking start function resolved")
        return False

    logger.info("[RANKING RUNTIME] call existing ranking start function: %s", fn)

    try:
        result = fn()
        logger.info("[RANKING RUNTIME] existing ranking start returned: %r", result)
        return True
    except TypeError:
        # register系で schedule を要求する実装に対応
        try:
            result = fn(schedule)
            logger.info("[RANKING RUNTIME] existing ranking start returned with schedule: %r", result)
            return True
        except Exception:
            logger.exception("[RANKING RUNTIME] ranking start failed with schedule")
            return False
    except Exception:
        logger.exception("[RANKING RUNTIME] ranking start failed")
        return False


def run_forever() -> int:
    logger.info("[RANKING RUNTIME] START")

    ok = start_existing_ranking_collector()
    if not ok:
        logger.error("[RANKING RUNTIME] abort because ranking collector could not start")
        return 1

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

        time.sleep(1.0)
