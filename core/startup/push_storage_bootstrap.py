# ============================================================
# File   : core/startup/push_storage_bootstrap.py
# Ver    : PRODUCTION-STABLE-REV2.0-PUSH-STORAGE-SINGLETON-START
# ------------------------------------------------------------
# 【概要】
#   PUSH tick DB 保存 writer の起動 bootstrap
#
# 【目的】
#   - startup.py から明示的に PUSH DB writer を起動する
#   - trading.push.push_db_writer.stream_writer singleton を優先使用
#   - push_stream 側と保存 writer インスタンスが分裂する問題を防ぐ
#   - order_book writer だけ起動して stream_data writer が起動しない問題を防ぐ
#
# 【重要】
#   - push_bootstrap は既存PUSH読み込み/初期化系
#   - push_storage_bootstrap はリアルタイムPUSH保存 writer 起動系
#   - 両者は別物
# ============================================================

from __future__ import annotations

import logging
import threading
from typing import Optional, Any

logger = logging.getLogger(__name__)

_storage_lock = threading.RLock()
_stream_writer_instance: Optional[Any] = None


def _resolve_stream_writer(buffer_size: int = 100):
    """
    既存 singleton を優先して取得する。

    push_db_writer.py 末尾に stream_writer = StreamDBWriter() があるため、
    原則それを使う。
    """
    try:
        from trading.push import push_db_writer as mod

        writer = getattr(mod, "stream_writer", None)
        if writer is not None:
            return writer

        cls = getattr(mod, "StreamDBWriter", None)
        if callable(cls):
            return cls(buffer_size=buffer_size)

    except Exception:
        logger.exception("[PushStorage] resolve stream_writer failed")

    return None


def _is_writer_alive(writer: Any) -> bool:
    try:
        th = getattr(writer, "_thread", None)
        return bool(getattr(writer, "_started", False) and th is not None and th.is_alive())
    except Exception:
        return False


def start_push_storage(buffer_size: int = 100) -> None:
    """
    PUSH DB writer を起動する。

    正常なら以下ログが出る:
      [StreamDB] connected → ...pushYYYYMMDD.db
      [StreamDB] writer loop started
    """
    global _stream_writer_instance

    with _storage_lock:
        try:
            if _stream_writer_instance is not None and _is_writer_alive(_stream_writer_instance):
                logger.info("[PushStorage] already running")
                return

            logger.info("🚀 Starting Push Storage System")

            writer = _resolve_stream_writer(buffer_size=buffer_size)
            if writer is None:
                logger.error("[PushStorage] stream_writer resolve failed")
                return

            # buffer_size を変更できる実装なら反映
            try:
                if hasattr(writer, "buffer_size"):
                    writer.buffer_size = int(buffer_size)
            except Exception:
                logger.debug("[PushStorage] buffer_size update skipped", exc_info=True)

            if _is_writer_alive(writer):
                logger.info("[PushStorage] stream_writer already alive")
            else:
                writer.start()

            _stream_writer_instance = writer

            logger.info(
                "✅ Push Storage System started writer=%s alive=%s",
                type(writer).__name__,
                _is_writer_alive(writer),
            )

        except Exception:
            logger.exception("❌ Push Storage start failed")


def is_push_storage_running() -> bool:
    with _storage_lock:
        return bool(_stream_writer_instance is not None and _is_writer_alive(_stream_writer_instance))


def get_stream_writer():
    with _storage_lock:
        return _stream_writer_instance


def stop_push_storage() -> None:
    global _stream_writer_instance

    with _storage_lock:
        writer = _stream_writer_instance
        if writer is None:
            return

        try:
            logger.info("[PushStorage] stopping")

            stop = getattr(writer, "stop", None)
            if callable(stop):
                stop()

            _stream_writer_instance = None
            logger.info("[PushStorage] stopped")

        except Exception:
            logger.exception("[PushStorage] stop failed")


__all__ = [
    "start_push_storage",
    "stop_push_storage",
    "is_push_storage_running",
    "get_stream_writer",
]