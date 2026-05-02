# ============================================================
# File   : scheduler_jobs/yahoo_jobs.py
# Version: Ver1.0-YAHOO-JOBS-PRODUCTION
# ------------------------------------------------------------
# ✔ Yahoo 1分補完
# ✔ 起動時補完
# ✔ scheduler用 job
# ✔ exception safe
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging

from trading.yahoo.scheduler.complement_scheduler import (
    yahoo_minutely_complement_job,
    run_yahoo_complement_once,
)

logger = logging.getLogger(__name__)


# ============================================================
# Startup Yahoo Complement
# ============================================================

def job_startup_yahoo_complement():
    """
    起動時 Yahoo 補完
    """

    try:

        run_yahoo_complement_once()

        logger.info("[YAHOO] startup complement executed")

    except Exception:

        logger.exception("[job_startup_yahoo_complement]")


# ============================================================
# Minutely Yahoo Complement
# ============================================================

def job_yahoo_complement():
    """
    1分Yahoo補完
    """

    try:

        yahoo_minutely_complement_job()

    except Exception:

        logger.exception("[job_yahoo_complement]")