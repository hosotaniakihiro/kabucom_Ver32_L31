# ============================================================
# File   : trading/push/push_stream/runtime.py
# Version: Ver1.0-PUSH-STREAM-RUNTIME
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional

import pandas as pd

from .constants import DEFAULT_WS_URL
from . import state

logger = logging.getLogger(__name__)

try:
    from global_state import global_data
except Exception:
    global_data = None


def _now() -> dt.datetime:
    return dt.datetime.now()


def _safe_iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        if isinstance(v, dt.datetime):
            return v.isoformat()
        if isinstance(v, dt.date):
            return dt.datetime.combine(v, dt.time()).isoformat()
        s = str(v).strip()
        return s if s else None
    except Exception:
        return None


def _safe_set_runtime(name: str, value: Any) -> None:
    try:
        if global_data is not None:
            setattr(global_data, name, value)
    except Exception:
        logger.debug("[push_stream] global_data setattr failed: %s", name, exc_info=True)


def _safe_get_runtime(name: str, default: Any = None) -> Any:
    try:
        if global_data is not None and hasattr(global_data, name):
            return getattr(global_data, name)
    except Exception:
        logger.debug("[push_stream] global_data getattr failed: %s", name, exc_info=True)
    return default


def _resolve_ws_url() -> str:
    for candidate in [
        _safe_get_runtime("push_ws_url"),
        os.getenv("PUSH_WS_URL"),
        DEFAULT_WS_URL,
    ]:
        if candidate:
            return str(candidate).strip()
    return DEFAULT_WS_URL


def _ensure_runtime_flags() -> None:
    _safe_set_runtime("ws_connected", False)
    _safe_set_runtime("push_stream_running", False)
    _safe_set_runtime("subscription_refresh_running", False)
    _safe_set_runtime("push_writer_running", False)
    _safe_set_runtime("last_push_received_at", None)
    _safe_set_runtime("last_push_db_flush_at", None)
    _safe_set_runtime("push_ws_url", _resolve_ws_url())


def _sync_push_df_to_global() -> None:
    try:
        if global_data is None:
            return

        setter = getattr(global_data, "set_push_df", None)
        if not callable(setter):
            logger.debug("[push_stream] global_data.set_push_df unavailable")
            return

        with state._df_lock:
            df = state._push_df.copy() if isinstance(state._push_df, pd.DataFrame) else pd.DataFrame()

        setter(df)
    except Exception:
        logger.exception("[push_stream] sync push_df to global failed")