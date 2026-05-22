# ============================================================
# File   : core/startup/push_db_writer_owner_guard_patch.py
# Version: V1.0-PUSH-DB-WRITER-OWNER-GUARD
# ------------------------------------------------------------
# 目的:
#   main.py / main_database.py 分離運用時に、main.py側から
#   PUSH DB writer が直接起動・書き込みされる経路を止める。
#
# 背景:
#   start_push_storage() は split_mode で no-op 化済みだが、
#   trading.push.push_db_writer.stream_writer が別経路から直接
#   start() / add_push_row() / flush() されると main.py 側でも
#   pushYYYYMMDD.db へ接続・保存してしまう。
#
# 方針:
#   - data_collector process では通常通り保存を許可
#   - main.py 側、かつ AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1 では
#     start / add_push_row / add_latest_push / flush を no-op 化
#   - リアルタイムPUSH DataFrame利用やエントリー判断は止めない
# ============================================================

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_LAST_LOG_TS = 0.0


def _should_skip_push_db_writer_here() -> bool:
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def _log_skip_once(action: str, extra: dict[str, Any] | None = None) -> None:
    global _LAST_LOG_TS
    now = time.time()
    if now - _LAST_LOG_TS < 10.0:
        return
    _LAST_LOG_TS = now
    try:
        from data_collectors.split_mode import is_data_collector_process, external_data_collectors_enabled
        logger.warning(
            "[PUSH DB WRITER OWNER GUARD] skip action=%s is_data_collector=%s external_collectors=%s reason=main_database_handles_push_storage extra=%s",
            action,
            bool(is_data_collector_process()),
            bool(external_data_collectors_enabled()),
            extra or {},
        )
    except Exception:
        logger.warning(
            "[PUSH DB WRITER OWNER GUARD] skip action=%s reason=main_database_handles_push_storage extra=%s",
            action,
            extra or {},
        )


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        from trading.push import push_db_writer as mod
    except Exception:
        logger.exception("[PUSH DB WRITER OWNER GUARD] import push_db_writer failed")
        return False

    cls = getattr(mod, "StreamDBWriter", None)
    if cls is None:
        logger.warning("[PUSH DB WRITER OWNER GUARD] StreamDBWriter missing")
        return False

    if getattr(cls, "_owner_guard_wrapped_v1", False):
        _INSTALLED = True
        return True

    old_start = getattr(cls, "start", None)
    old_add_push_row = getattr(cls, "add_push_row", None)
    old_add_latest_push = getattr(cls, "add_latest_push", None)
    old_flush = getattr(cls, "flush", None)

    if callable(old_start):
        def start_guarded(self, *args, **kwargs):
            if _should_skip_push_db_writer_here():
                try:
                    self._started = False
                except Exception:
                    pass
                _log_skip_once("start")
                return None
            return old_start(self, *args, **kwargs)
        cls.start = start_guarded

    if callable(old_add_push_row):
        def add_push_row_guarded(self, row, *args, **kwargs):
            if _should_skip_push_db_writer_here():
                _log_skip_once("add_push_row", {"symbol": (row or {}).get("symbol") if isinstance(row, dict) else None})
                return None
            return old_add_push_row(self, row, *args, **kwargs)
        cls.add_push_row = add_push_row_guarded

    if callable(old_add_latest_push):
        def add_latest_push_guarded(self, *args, **kwargs):
            if _should_skip_push_db_writer_here():
                _log_skip_once("add_latest_push")
                return None
            return old_add_latest_push(self, *args, **kwargs)
        cls.add_latest_push = add_latest_push_guarded

    if callable(old_flush):
        def flush_guarded(self, *args, **kwargs):
            if _should_skip_push_db_writer_here():
                try:
                    with self.lock:
                        if getattr(self, "buffer", None):
                            self.buffer.clear()
                except Exception:
                    pass
                _log_skip_once("flush")
                return True
            return old_flush(self, *args, **kwargs)
        cls.flush = flush_guarded

    cls._owner_guard_wrapped_v1 = True
    cls._owner_guard_originals_v1 = {
        "start": old_start,
        "add_push_row": old_add_push_row,
        "add_latest_push": old_add_latest_push,
        "flush": old_flush,
    }

    try:
        writer = getattr(mod, "stream_writer", None)
        if writer is not None and _should_skip_push_db_writer_here():
            stop = getattr(writer, "stop", None)
            th = getattr(writer, "_thread", None)
            if callable(stop) and th is not None and getattr(th, "is_alive", lambda: False)():
                logger.warning("[PUSH DB WRITER OWNER GUARD] stopping already-running writer in main process")
                stop()
    except Exception:
        logger.debug("[PUSH DB WRITER OWNER GUARD] stop already-running writer skipped", exc_info=True)

    _INSTALLED = True
    logger.warning(
        "[PUSH DB WRITER OWNER GUARD] installed skip_here=%s",
        _should_skip_push_db_writer_here(),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[PUSH DB WRITER OWNER GUARD] auto install failed")

__all__ = ["install"]
