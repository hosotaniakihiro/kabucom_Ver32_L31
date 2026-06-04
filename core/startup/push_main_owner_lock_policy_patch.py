# ============================================================
# File   : core/startup/push_main_owner_lock_policy_patch.py
# Version: V1.0-PUSH-MAIN-OWNER-LOCK-POLICY
# ------------------------------------------------------------
# 目的:
#   main_database.py / data collector 系が kabu Station PUSH WebSocket の
#   single-owner lock を握らないようにする。
#
# 背景:
#   DB系プロセスが先に PUSH lock を取得すると、main.py 側が
#   connected=False / total_received=0 のままになり、エントリー判断用の
#   PUSHサマリーが空になる。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False


def _is_database_collector_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in (
            "main_database.py",
            "db_prepare_runner.py",
            "ranking_collector_runner.py",
            "push_receiver_runner.py",
            "yahoo_complement_runner.py",
            "summary_database_runner.py",
            "data_collectors_runner.py",
        )):
            return True
        for key in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
        ):
            if os.getenv(key) == "1":
                return True
    except Exception:
        pass
    return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    os.environ.setdefault("PUSH_STREAM_MAIN_OWNER_POLICY", "1")
    os.environ.setdefault("PUSH_STREAM_DB_PROCESS_SKIP_WS_OWNER", "1")
    os.environ.setdefault("PUSH_STREAM_SINGLE_OWNER_WAIT_RETRY", "1")
    os.environ.setdefault("PUSH_STREAM_SINGLE_OWNER_RETRY_SEC", "5.0")

    try:
        from core.startup import push_stream_reconnect_stability_patch as p
    except Exception:
        logger.exception("[PUSH MAIN OWNER POLICY] stability patch import failed")
        return False

    old_try = getattr(p, "_try_acquire_single_owner_lock", None)
    if not callable(old_try):
        logger.warning("[PUSH MAIN OWNER POLICY] _try_acquire_single_owner_lock unavailable")
        return False
    if getattr(old_try, "_push_main_owner_policy_v1", False):
        _INSTALLED = True
        return True

    def _try_acquire_single_owner_lock_main_owner() -> Tuple[bool, Any, dict[str, Any]]:
        if os.environ.get("PUSH_STREAM_DB_PROCESS_SKIP_WS_OWNER", "1").strip().lower() in {"1", "true", "yes", "on"}:
            if _is_database_collector_context():
                detail = {
                    "db_context": True,
                    "argv": sys.argv,
                    "reason": "db_process_must_not_own_push_ws",
                }
                logger.warning(
                    "[PUSH MAIN OWNER POLICY] DB/data collector context -> skip PUSH WebSocket owner detail=%s",
                    detail,
                )
                return False, None, detail
        return old_try()

    _try_acquire_single_owner_lock_main_owner._push_main_owner_policy_v1 = True  # type: ignore[attr-defined]
    _try_acquire_single_owner_lock_main_owner._original = old_try  # type: ignore[attr-defined]
    p._try_acquire_single_owner_lock = _try_acquire_single_owner_lock_main_owner

    _INSTALLED = True
    logger.warning(
        "[PUSH MAIN OWNER POLICY] installed db_context=%s argv=%s",
        _is_database_collector_context(),
        sys.argv,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[PUSH MAIN OWNER POLICY] auto install failed")


__all__ = ["install"]
