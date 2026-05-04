# ============================================================
# File   : core/startup/scheduler_ranking_bootstrap.py
# Version: FINAL-PRODUCTION-REV1.0-SCHEDULER-RANKING-BOOTSTRAP
# ------------------------------------------------------------
# 【概要】
#   ranking DB writer 明示起動。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

from global_state import global_data

logger = logging.getLogger(__name__)


def start_ranking_db_writer_safe() -> bool:
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
        logger.info("[startup.scheduler_startup] ranking db writer started writer=%s", type(writer).__name__)
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


__all__ = ["start_ranking_db_writer_safe"]
