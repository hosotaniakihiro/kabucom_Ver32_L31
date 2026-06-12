# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "REV1-PUSH-STREAM-DATA-RECEIVED-AT-DATETIME"
_INSTALLED = False


def _env_on(name: str, default: bool = True) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        if os.environ.get("DISABLE_PUSH_STREAM_DATA_RECEIVED_AT_DATETIME_PATCH", "").strip() == "1":
            logger.warning("[PUSH STREAM DATA RECEIVED_AT PATCH] disabled")
            return False

        from trading.push.push_db_writer import StreamDBWriter  # type: ignore

        if getattr(StreamDBWriter, "_received_at_datetime_patch_installed", False):
            _INSTALLED = True
            return True

        original = StreamDBWriter._resolve_push_datetime_value

        def patched(self: Any, row: dict) -> Any:
            if _env_on("PUSH_STREAM_DATA_USE_RECEIVED_AT_DATETIME", True):
                try:
                    received_at = row.get("received_at") or row.get("recv_at") or row.get("received_time")
                    if received_at:
                        dt_obj = self._coerce_datetime_obj(received_at)
                        if dt_obj is not None:
                            try:
                                now_local = self._now_local()
                                if getattr(dt_obj, "tzinfo", None) is None:
                                    dt_obj = dt_obj.replace(tzinfo=now_local.tzinfo)
                            except Exception:
                                pass
                            return dt_obj
                        return received_at
                except Exception:
                    logger.exception("[PUSH STREAM DATA RECEIVED_AT PATCH] fallback original")
            return original(self, row)

        StreamDBWriter._resolve_push_datetime_value = patched
        StreamDBWriter._received_at_datetime_patch_installed = True
        _INSTALLED = True
        logger.warning("[PUSH STREAM DATA RECEIVED_AT PATCH] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[PUSH STREAM DATA RECEIVED_AT PATCH] install failed")
        return False


__all__ = ["VERSION", "install"]
