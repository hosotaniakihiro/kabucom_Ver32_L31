# ============================================================
# File   : trading/push/push_stream/state.py
# Version: Ver1.0-PUSH-STREAM-STATE
# ============================================================

from __future__ import annotations

import datetime as dt
import queue
import threading
from collections import deque
from typing import Any, Callable, Optional

import pandas as pd
import websocket

from .constants import MAX_DF_ROWS

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

# ------------------------------------------------------------
# 2026-09-02 P0: on_message を O(1) にするための tick リング。
#
# 旧実装は on_message のたびに
#   pd.concat([累積 df, 1行]) -> _sync_push_df_to_global()
# を実行しており、1 tick あたりのコストが累積行数に比例していた。
# 実測 (2026-09-02 本番 push_receiver):
#   df 2,000 行 -> 21.7 msg/s / df 12,400 行 -> 1.1 msg/s
# WS の読み取りスレッドが register スナップショットのバーストを
# 捌けなくなり、kabu Station 側から 3 秒で切断され続けた
# (RECONNECT STORM: fast_death 55 連続)。
#
# dict を deque へ入れるだけにして append を O(1) にする。
# DataFrame 化は参照側 (get_push_dataframe) で遅延生成しキャッシュする。
# global_data への同期は専用スレッドが間引いて行う。
# ------------------------------------------------------------
_push_rows: "deque[dict]" = deque(maxlen=MAX_DF_ROWS)
_push_rows_seq: int = 0          # append 済み総数 (materialize 判定用)
_push_df_cache: Optional[pd.DataFrame] = None
_push_df_cache_seq: int = -1
_gsync_thread: Optional[threading.Thread] = None
_gsync_last_at: float = 0.0
_gsync_last_seq: int = -1
_gsync_last_elapsed: float = 0.0
_gsync_merged_upto_seq: int = -1   # merged へ折り込み済みの _push_rows_seq
_gsync_last_out = None             # 前回 global_data へ渡した merged (同一性で判定)

_ring_buffer = None
_stream_writer = None
_order_book_writer = None

_last_message_at: Optional[dt.datetime] = None
_last_flush_at: Optional[dt.datetime] = None
_last_error_at: Optional[dt.datetime] = None
_last_connect_at: Optional[dt.datetime] = None
_last_disconnect_at: Optional[dt.datetime] = None

# 2026-10-29: 直前の切断の理由。ws_callbacks (on_error/on_close) と monitor (自己切断) が書き、
#   runner が接続ごとに取り出して 1 行のログにする。
#   keys: cause / error / close_code / close_msg / self_close_reason
_disconnect_info: dict = {}

_total_received = 0
_total_flushed = 0
_total_dropped = 0
_total_errors = 0

_rotation_enabled = False

# REST 銘柄登録の成否。WS stale watchdog が「WSではなく登録が壊れている」
# ケースで無駄に close して再接続ループを作らないために使う。
_last_register_ok_at: Optional[dt.datetime] = None
_last_register_fail_at: Optional[dt.datetime] = None