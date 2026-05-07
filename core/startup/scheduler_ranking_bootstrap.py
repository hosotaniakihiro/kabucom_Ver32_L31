# ============================================================
# File   : core/startup/scheduler_ranking_bootstrap.py
# Version: FINAL-PRODUCTION-REV1.1-SCHEDULER-RANKING-BOOTSTRAP-SPLIT-MODE
# ------------------------------------------------------------
# 【概要】
#   ranking DB writer 明示起動。
#
# Split mode:
#   - main_database.py が ranking DB writer を担当する
#   - main.py 側では二重起動防止のため skip する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

from global_state import global_data

logger = logging.getLogger(__name__)


def _should_skip_ranking_writer_start_in_main() -> bool:
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def start_ranking_db_writer_safe() -> bool:
    if _should_skip_ranking_writer_start_in_main():
        logger.warning(
            "[startup.scheduler_startup] ranking db writer bootstrap skipped in main process because "
            "AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1; main_database.py handles ranking writer."
        )
        try:
            global_data.ranking_db_writer_bootstrap_done = False
            global_data.ranking_db_writer_bootstrap_skipped_external = True
            global_data.ranking_db_writer_bootstrap_failed = False
            global_data.ranking_db_writer_bootstrap_at = dt.datetime.now()
        except Exception:
            pass
        return True

    logger.info("[startup.scheduler_startup] ranking db writer bootstrap start")
    try:
        from trading.ranking.ranking_db_writer import ensure_ranking_writer_started
        writer = ensure_ranking_writer_started()
        try:
            global_data.ranking_db_writer_bootstrap_done = True
            global_data.ranking_db_writer_bootstrap_failed = False
            global_data.ranking_db_writer_bootstrap_skipped_external = False
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
