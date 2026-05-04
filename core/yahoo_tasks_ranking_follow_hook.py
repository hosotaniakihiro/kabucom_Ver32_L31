# ============================================================
# File   : core/yahoo_tasks_ranking_follow_hook.py
# Version: PRODUCTION-STABLE-YAHOO-RANKING-FOLLOW-HOOK-REV1.0
# ------------------------------------------------------------
# Purpose:
#   core/yahoo_tasks.py から呼ぶための軽量hook。
#   既存の重い全件Yahoo補完の代わり、または前段に追加して使う。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUNNING = False
_LAST_STARTED_AT: Optional[dt.datetime] = None
STALE_SECONDS = 90.0


def run_yahoo_ranking_follow_job_safe(*, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    global _RUNNING, _LAST_STARTED_AT
    now = now or dt.datetime.now()

    with _LOCK:
        if _RUNNING:
            elapsed = (now - _LAST_STARTED_AT).total_seconds() if _LAST_STARTED_AT else 0.0
            if elapsed < STALE_SECONDS:
                logger.warning("[YAHOO RANKING FOLLOW HOOK] skipped because previous still running elapsed=%.1fs", elapsed)
                return {"status": "skipped_running", "elapsed": elapsed}
            logger.warning("[YAHOO RANKING FOLLOW HOOK] stale running reset elapsed=%.1fs", elapsed)
        _RUNNING = True
        _LAST_STARTED_AT = now

    try:
        from trading.yahoo.ranking_follow import run_yahoo_ranking_follow_once
        ret = run_yahoo_ranking_follow_once(now=now)
        ret["status"] = "ok"
        return ret
    except Exception as e:
        logger.exception("[YAHOO RANKING FOLLOW HOOK] failed")
        return {"status": "error", "error": str(e)}
    finally:
        with _LOCK:
            _RUNNING = False
