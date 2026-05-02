# ============================================================
# File   : trading/push/push_stream/dataframe.py
# Version: Ver1.0-PUSH-STREAM-DATAFRAME
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .constants import MAX_DF_ROWS
from . import state
from .runtime import _sync_push_df_to_global

logger = logging.getLogger(__name__)

try:
    from trading.push.push_ring_buffer import PushRingBuffer
except Exception:
    PushRingBuffer = None


def get_push_dataframe() -> pd.DataFrame:
    with state._df_lock:
        return state._push_df.copy() if state._push_df is not None and not state._push_df.empty else pd.DataFrame()


def clear_push_dataframe() -> None:
    with state._df_lock:
        state._push_df = pd.DataFrame()

    _sync_push_df_to_global()
    logger.info("[push_stream] push dataframe cleared")


def _append_df(row: dict) -> None:
    try:
        with state._df_lock:
            add_df = pd.DataFrame([row])
            if state._push_df is None or state._push_df.empty:
                state._push_df = add_df
            else:
                state._push_df = pd.concat([state._push_df, add_df], ignore_index=True)

            if len(state._push_df) > MAX_DF_ROWS:
                state._push_df = state._push_df.tail(MAX_DF_ROWS).reset_index(drop=True)

        _sync_push_df_to_global()

    except Exception:
        logger.exception("[push_stream] dataframe append failed")


def _init_ring_buffer():
    try:
        if PushRingBuffer is not None:
            return PushRingBuffer(maxlen=MAX_DF_ROWS)
    except Exception:
        logger.exception("[push_stream] PushRingBuffer init failed")
    return None