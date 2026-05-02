# ============================================================
# File   : core/global_context/push_state.py
# Version: V33-FINAL-PUSH-STATE-ORDERFLOW-SAFE
# ------------------------------------------------------------
# ✔ latest_tick 保存
# ✔ スレッド安全
# ✔ bulk更新 / snapshot
# ✔ orderflow 正式API化
# ✔ orderflow 上限管理
# ✔ 直接属性アクセス禁止設計
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Any, Optional, List
from collections import defaultdict
from threading import Lock

logger = logging.getLogger(__name__)


class PushState:

    # ============================================================
    # INIT
    # ============================================================

    def __init__(self):
        self._lock = Lock()
        self._latest_ticks: Dict[str, Dict[str, Any]] = {}

        # ---- orderflow ----
        self._orderflow_lock = Lock()
        self._orderflow_buffer: Dict[str, List[dict]] = defaultdict(list)

        # メモリ暴走防止
        self._orderflow_maxlen = 1000


    # ============================================================
    # ================= TICK =================
    # ============================================================

    def set_tick(self, symbol: str, tick: dict):
        try:
            if not isinstance(tick, dict):
                return
            with self._lock:
                self._latest_ticks[str(symbol)] = tick
        except Exception:
            logger.exception("PushState.set_tick failed")

    def set_ticks_bulk(self, ticks: Dict[str, dict]):
        try:
            with self._lock:
                for sym, tick in (ticks or {}).items():
                    if isinstance(tick, dict):
                        self._latest_ticks[str(sym)] = tick
        except Exception:
            logger.exception("PushState.set_ticks_bulk failed")

    def get_tick(self, symbol: str) -> Optional[dict]:
        try:
            return self._latest_ticks.get(str(symbol))
        except Exception:
            return None

    def snapshot(self) -> Dict[str, dict]:
        try:
            with self._lock:
                return dict(self._latest_ticks)
        except Exception:
            return {}

    def remove(self, symbol: str):
        try:
            with self._lock:
                self._latest_ticks.pop(str(symbol), None)
        except Exception:
            logger.exception("PushState.remove failed")

    def clear(self):
        try:
            with self._lock:
                self._latest_ticks.clear()
        except Exception:
            logger.exception("PushState.clear failed")

    def size(self) -> int:
        try:
            return len(self._latest_ticks)
        except Exception:
            return 0


    # ============================================================
    # ================= ORDERFLOW =================
    # ============================================================

    def append_orderflow(self, symbol: str, row: dict):
        """
        tick蓄積（AI用 microstructure）
        """
        try:
            if not isinstance(row, dict):
                return

            with self._orderflow_lock:
                buf = self._orderflow_buffer[str(symbol)]
                buf.append(row)

                # 上限管理
                if len(buf) > self._orderflow_maxlen:
                    buf.pop(0)

        except Exception:
            logger.exception("PushState.append_orderflow failed")

    def get_orderflow(self, symbol: str) -> List[dict]:
        """
        コピー返却（安全）
        """
        try:
            with self._orderflow_lock:
                return list(self._orderflow_buffer.get(str(symbol), []))
        except Exception:
            return []

    def clear_orderflow(self, symbol: Optional[str] = None):
        try:
            with self._orderflow_lock:
                if symbol is None:
                    self._orderflow_buffer.clear()
                else:
                    self._orderflow_buffer.pop(str(symbol), None)
        except Exception:
            logger.exception("PushState.clear_orderflow failed")

    def orderflow_size(self, symbol: str) -> int:
        try:
            with self._orderflow_lock:
                return len(self._orderflow_buffer.get(str(symbol), []))
        except Exception:
            return 0