# ============================================================
# File   : trading/push/subscription_manager/state.py
# Function:
#   - subscription manager の共有状態を一元管理する
#   - lock / thread / stop_event / ws sender / refresh callable
#   - last registered symbols / refresh timestamp を保持する
# ------------------------------------------------------------
# Notes:
#   - 副作用の強い状態をこのファイルへ集約
#   - 他モジュールはこの state を参照して連携する
# ============================================================

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

manager_lock = threading.RLock()
manager_thread: Optional[threading.Thread] = None
manager_stop_event = threading.Event()

refresh_callable: Optional[Callable[..., Any]] = None
ws_sender_cache: Optional[Callable[..., Any]] = None

last_registered_symbols: list[str] = []
last_refresh_ts: float = 0.0
started: bool = False
last_refresh_reason_ts: dict[str, float] = {}
last_refresh_target_fingerprint: str = ""