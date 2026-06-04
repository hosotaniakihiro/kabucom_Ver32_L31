# ============================================================
# File   : core/startup/push_main_owner_lock_policy_patch.py
# Version: V1.1-ALLOW-PUSH-RECEIVER-OWNER
# ------------------------------------------------------------
# 目的:
#   main_database.py / data collector 系のうち、PUSH受信専用ではない
#   プロセスが kabu Station PUSH WebSocket の single-owner lock を握らないようにする。
#
# V1.1:
#   - push_receiver_runner.py はPUSH受信専用プロセスなので、DB/data collector
#     文脈でも WebSocket owner を許可する。
#   - V1.0では push_receiver_runner.py まで
#       DB/data collector context -> skip PUSH WebSocket owner
#     になり、PUSHが完全に受信できなくなっていた。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        return ""


def _is_push_receiver_context() -> bool:
    try:
        argv = _argv_text()
        if "push_receiver_runner.py" in argv:
            return True
        if os.getenv("AUTOSTOCK_PUSH_RECEIVER_PROCESS") == "1":
            return True
        if os.getenv("AUTOSTOCK_PUSH_WS_OWNER") == "1":
            return True
        role = str(os.getenv("AUTOSTOCK_PROCESS_ROLE") or os.getenv("AUTOSTOCK_ROLE") or "").strip().lower()
        if role in {"push_receiver", "push", "push_ws_owner"}:
            return True
    except Exception:
        pass
    return False


def _is_database_collector_context() -> bool:
    try:
        if _is_push_receiver_context():
            return False
        argv = _argv_text()
        if any(x in argv for x in (
            "main_database.py",
            "db_prepare_runner.py",
            "ranking_collector_runner.py",
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
    if getattr(old_try, "_push_main_owner_policy_v11", False):
        _INSTALLED = True
        return True

    # 既にV1.0でwrap済みなら、元関数へ戻してからV1.1で包む。
    original = getattr(old_try, "_original", old_try)

    def _try_acquire_single_owner_lock_main_owner() -> Tuple[bool, Any, dict[str, Any]]:
        if _is_push_receiver_context():
            logger.warning(
                "[PUSH MAIN OWNER POLICY] push_receiver context -> allow PUSH WebSocket owner argv=%s",
                sys.argv,
            )
            return original()
        if os.environ.get("PUSH_STREAM_DB_PROCESS_SKIP_WS_OWNER", "1").strip().lower() in {"1", "true", "yes", "on"}:
            if _is_database_collector_context():
                detail = {
                    "db_context": True,
                    "push_receiver": False,
                    "argv": sys.argv,
                    "reason": "db_process_must_not_own_push_ws",
                }
                logger.warning(
                    "[PUSH MAIN OWNER POLICY] DB/data collector context -> skip PUSH WebSocket owner detail=%s",
                    detail,
                )
                return False, None, detail
        return original()

    _try_acquire_single_owner_lock_main_owner._push_main_owner_policy_v1 = True  # type: ignore[attr-defined]
    _try_acquire_single_owner_lock_main_owner._push_main_owner_policy_v11 = True  # type: ignore[attr-defined]
    _try_acquire_single_owner_lock_main_owner._original = original  # type: ignore[attr-defined]
    p._try_acquire_single_owner_lock = _try_acquire_single_owner_lock_main_owner

    _INSTALLED = True
    logger.warning(
        "[PUSH MAIN OWNER POLICY] installed v1.1 db_context=%s push_receiver=%s argv=%s",
        _is_database_collector_context(),
        _is_push_receiver_context(),
        sys.argv,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[PUSH MAIN OWNER POLICY] auto install failed")


__all__ = ["install"]
