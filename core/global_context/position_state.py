# ============================================================
# File   : core/global_context/position_state.py
# Version: V40-FINAL-POSITION-STATE-LEGACY-COMPAT
# ------------------------------------------------------------
# ✔ V39 全機能完全保持（削除ゼロ）
# ✔ entry_inflight 公開互換
# ✔ entry_inflight_lock 旧互換追加
# ✔ thread safe
# ✔ snapshot安全
# ✔ 例外安全
# ✔ ThinLayer完全対応
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from threading import Lock
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

ENTRY_INFLIGHT_TIMEOUT_SEC = 30


class PositionState:

    def __init__(self):

        self._lock = Lock()

        # =====================================================
        # POSITIONS
        # =====================================================
        self.positions: Dict[str, dict] = {}
        self.open_positions: Dict[str, dict] = {}

        # =====================================================
        # ENTRY INFLIGHT
        # =====================================================
        self._entry_inflight_lock = Lock()
        self._entry_inflight = set()
        self._entry_info: Dict[str, dict] = {}
        self._cooldown_until: Dict[str, dt.datetime] = {}

        # =====================================================
        # SYMBOL CLUSTER
        # =====================================================
        self._symbol_cluster: Dict[str, int] = {}

        # =====================================================
        # EXIT METRICS
        # =====================================================
        self._mfe: Dict[str, float] = {}
        self._mae: Dict[str, float] = {}
        self._trailing_price: Dict[str, float] = {}

    # ========================================================
    # 旧互換プロパティ
    # ========================================================

    @property
    def entry_inflight(self):
        """
        旧設計互換:
        global_data.entry_inflight
        """
        return self._entry_inflight

    @property
    def entry_inflight_lock(self):
        """
        旧設計互換:
        global_data.entry_inflight_lock
        """
        return self._entry_inflight_lock

    # ========================================================
    # POSITION
    # ========================================================

    def set(self, symbol: str, data: dict):
        if not isinstance(data, dict):
            return
        with self._lock:
            self.positions[str(symbol)] = dict(data)

    def set_open(self, symbol: str, data: dict):
        if not isinstance(data, dict):
            return
        with self._lock:
            self.open_positions[str(symbol)] = dict(data)

    def get(self, symbol: str) -> Optional[dict]:
        row = self.positions.get(str(symbol))
        return dict(row) if isinstance(row, dict) else None

    def get_open(self, symbol: str) -> Optional[dict]:
        row = self.open_positions.get(str(symbol))
        return dict(row) if isinstance(row, dict) else None

    def remove(self, symbol: str):
        with self._lock:
            s = str(symbol)
            self.positions.pop(s, None)
            self.open_positions.pop(s, None)
            self._mfe.pop(s, None)
            self._mae.pop(s, None)
            self._trailing_price.pop(s, None)

    def snapshot_dict(self):
        with self._lock:
            return {k: dict(v) for k, v in self.open_positions.items()}

    # ========================================================
    # 旧互換API（EXIT LOOP用）
    # ========================================================

    def snapshot_open(self):
        """
        旧API互換:
        GC.positions.snapshot_open()
        """
        return self.snapshot_dict()


    def size(self) -> int:
        return len(self.open_positions)

    # ========================================================
    # ENTRY INFLIGHT
    # ========================================================

    def add_entry_inflight(self, symbol: str, order_id: str, side: str):
        if not symbol:
            return

        now = dt.datetime.now()
        s = str(symbol)

        with self._entry_inflight_lock:
            self._entry_inflight.add(s)
            self._entry_info[s] = {
                "order_id": order_id,
                "side": side,
                "ts": now,
            }

    def release_entry_inflight(self, symbol: str, reason: str | None = None):
        s = str(symbol)

        with self._entry_inflight_lock:
            self._entry_inflight.discard(s)
            self._entry_info.pop(s, None)

        if reason:
            logger.info("[ENTRY_INFLIGHT] released %s reason=%s", s, reason)

    def is_entry_inflight(self, symbol: str) -> bool:
        return str(symbol) in self._entry_inflight

    def cleanup_entry_inflight(self):
        now = dt.datetime.now()
        expired = []

        with self._entry_inflight_lock:
            for symbol, info in list(self._entry_info.items()):
                ts = info.get("ts")
                if not ts:
                    continue
                if (now - ts).total_seconds() > ENTRY_INFLIGHT_TIMEOUT_SEC:
                    expired.append(symbol)

            for symbol in expired:
                self._entry_inflight.discard(symbol)
                self._entry_info.pop(symbol, None)

        if expired:
            logger.warning(
                "[ENTRY_INFLIGHT] auto released expired=%s",
                expired,
            )

    # ========================================================
    # COOLDOWN
    # ========================================================

    def set_cooldown(self, symbol: str, seconds: int):
        with self._lock:
            self._cooldown_until[str(symbol)] = (
                dt.datetime.now() + dt.timedelta(seconds=int(seconds))
            )

    def is_cooldown(self, symbol: str) -> bool:
        s = str(symbol)
        until = self._cooldown_until.get(s)

        if not until:
            return False

        if dt.datetime.now() > until:
            with self._lock:
                self._cooldown_until.pop(s, None)
            return False

        return True

    # ========================================================
    # SYMBOL CLUSTER
    # ========================================================

    def set_cluster(self, symbol: str, cluster_id: int):
        with self._lock:
            self._symbol_cluster[str(symbol)] = int(cluster_id)

    def get_cluster(self, symbol: str):
        return self._symbol_cluster.get(str(symbol))

    def remove_cluster(self, symbol: str):
        with self._lock:
            self._symbol_cluster.pop(str(symbol), None)

    def cluster_snapshot(self):
        with self._lock:
            return dict(self._symbol_cluster)

    # ========================================================
    # MFE / MAE
    # ========================================================

    def update_mfe_mae(self, symbol: str, pnl: float):
        s = str(symbol)
        with self._lock:
            prev_mfe = self._mfe.get(s, pnl)
            prev_mae = self._mae.get(s, pnl)

            self._mfe[s] = max(prev_mfe, pnl)
            self._mae[s] = min(prev_mae, pnl)

    def get_mfe(self, symbol: str) -> float:
        return self._mfe.get(str(symbol), 0.0)

    def get_mae(self, symbol: str) -> float:
        return self._mae.get(str(symbol), 0.0)

    # ========================================================
    # TRAILING
    # ========================================================

    def set_trailing_price(self, symbol: str, price: float):
        with self._lock:
            self._trailing_price[str(symbol)] = float(price)

    def get_trailing_price(self, symbol: str) -> Optional[float]:
        return self._trailing_price.get(str(symbol))

    # ========================================================
    # HOLD TIME
    # ========================================================

    def get_hold_seconds(self, symbol: str) -> float:
        pos = self.open_positions.get(str(symbol))
        if not pos:
            return 0.0

        entry_time = pos.get("entry_time")
        if not entry_time:
            return 0.0

        return (dt.datetime.now() - entry_time).total_seconds()

    # ========================================================
    # CLEAR
    # ========================================================

    def clear(self):
        with self._lock:
            self.positions.clear()
            self.open_positions.clear()
            self._entry_inflight.clear()
            self._entry_info.clear()
            self._cooldown_until.clear()
            self._symbol_cluster.clear()
            self._mfe.clear()
            self._mae.clear()
            self._trailing_price.clear()