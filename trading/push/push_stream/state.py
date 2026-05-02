# ============================================================
# File   : trading/push/push_stream/state.py
# Version: Ver1.0-PUSH-STREAM-STATE
# ============================================================

from __future__ import annotations

import datetime as dt
import queue
import threading
from typing import Any, Callable, Optional

import pandas as pd
import websocket

_ws_app: Optional[websocket.WebSocketApp] = None
_ws_thread: Optional[threading.Thread] = None
_flush_thread: Optional[threading.Thread] = None
_monitor_thread: Optional[threading.Thread] = None
_rotate_thread: Optional[threading.Thread] = None

_stop_event = threading.Event()
_connected_event = threading.Event()

_runtime_lock = threading.RLock()
_sender_lock = threading.RLock()
_df_lock = threading.RLock()
_ws_state_lock = threading.RLock()

_ws_sender: Optional[Callable[[str], Any]] = None
_refresh_callable: Optional[Callable[..., Any]] = None

_push_queue: "queue.Queue[dict]" = queue.Queue(maxsize=200000)
_push_df = pd.DataFrame()

_ring_buffer = None
_stream_writer = None
_order_book_writer = None

_last_message_at: Optional[dt.datetime] = None
_last_flush_at: Optional[dt.datetime] = None
_last_error_at: Optional[dt.datetime] = None
_last_connect_at: Optional[dt.datetime] = None
_last_disconnect_at: Optional[dt.datetime] = None

_total_received = 0
_total_flushed = 0
_total_dropped = 0
_total_errors = 0

_rotation_enabled = False