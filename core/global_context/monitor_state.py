# ============================================================
# File   : core/global_context/monitor_state.py
# Version: V37-FINAL-MONITOR-STATE-5SEC-SAFE-COPY
# ------------------------------------------------------------
# ✔ five_sec_bars 保持
# ✔ symbol_health 保持
# ✔ trade_restricted 統合
# ✔ クールダウン自動解除
# ✔ snapshot安全化（deep-ish copy返却）
# ✔ get_five_sec_bar も copy 返却
# ✔ clear対応
# ✔ 将来拡張対応
# ✔ スレッド安全
# ✔ 例外安全
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from threading import Lock
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class MonitorState:
    def __init__(self):
        self._lock = Lock()

        # ----------------------------------------------------
        # 5秒バー（symbol -> dict）
        # ----------------------------------------------------
        self.five_sec_bars: Dict[str, Dict[str, Any]] = {}

        # ----------------------------------------------------
        # symbol health / 将来拡張
        # ----------------------------------------------------
        self.symbol_health: Dict[str, Dict[str, Any]] = {}

        # ----------------------------------------------------
        # trade restricted（symbol -> datetime）
        # ----------------------------------------------------
        self._trade_restricted: Dict[str, dt.datetime] = {}

    # ========================================================
    # FIVE SEC BAR
    # ========================================================

    def set_five_sec_bar(self, symbol: str, bar: dict):
        try:
            if not symbol:
                return
            if not isinstance(bar, dict):
                return

            key = str(symbol)

            with self._lock:
                self.five_sec_bars[key] = dict(bar)

        except Exception:
            logger.exception("MonitorState.set_five_sec_bar failed")

    def get_five_sec_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            if not symbol:
                return None

            key = str(symbol)

            with self._lock:
                bar = self.five_sec_bars.get(key)
                return dict(bar) if isinstance(bar, dict) else None

        except Exception:
            return None

    def get_5sec_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        互換alias。
        exit_price_source.py 側が get_5sec_bar も探索するため用意。
        """
        return self.get_five_sec_bar(symbol)

    def get_latest_5sec_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        互換alias。
        exit_price_source.py 側が get_latest_5sec_bar も探索するため用意。
        """
        return self.get_five_sec_bar(symbol)

    def snapshot_five_sec_bars(self) -> Dict[str, dict]:
        try:
            with self._lock:
                return {
                    str(k): dict(v)
                    for k, v in self.five_sec_bars.items()
                    if isinstance(v, dict)
                }
        except Exception:
            return {}

    def clear_five_sec_bars(self):
        try:
            with self._lock:
                self.five_sec_bars.clear()
        except Exception:
            logger.exception("MonitorState.clear_five_sec_bars failed")

    # ========================================================
    # SYMBOL HEALTH
    # ========================================================

    def set_symbol_health(self, symbol: str, data: dict):
        try:
            if not symbol:
                return
            if not isinstance(data, dict):
                return

            with self._lock:
                self.symbol_health[str(symbol)] = dict(data)

        except Exception:
            logger.exception("MonitorState.set_symbol_health failed")

    def get_symbol_health(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            if not symbol:
                return None

            with self._lock:
                data = self.symbol_health.get(str(symbol))
                return dict(data) if isinstance(data, dict) else None

        except Exception:
            return None

    def snapshot_symbol_health(self) -> Dict[str, dict]:
        try:
            with self._lock:
                return {
                    str(k): dict(v)
                    for k, v in self.symbol_health.items()
                    if isinstance(v, dict)
                }
        except Exception:
            return {}

    # ========================================================
    # TRADE RESTRICTED
    # ========================================================

    def restrict_trade(self, symbol: str, seconds: int):
        """
        指定秒数、取引禁止
        """
        try:
            if not symbol:
                return

            until = dt.datetime.now() + dt.timedelta(seconds=int(seconds))

            with self._lock:
                self._trade_restricted[str(symbol)] = until

        except Exception:
            logger.exception("MonitorState.restrict_trade failed")

    def is_trade_restricted(self, symbol: str) -> bool:
        try:
            if not symbol:
                return False

            key = str(symbol)
            now = dt.datetime.now()

            with self._lock:
                until = self._trade_restricted.get(key)

                if not until:
                    return False

                if now > until:
                    self._trade_restricted.pop(key, None)
                    return False

                return True

        except Exception:
            return False

    def clear_trade_restriction(self, symbol: str):
        try:
            if not symbol:
                return

            with self._lock:
                self._trade_restricted.pop(str(symbol), None)

        except Exception:
            logger.exception("MonitorState.clear_trade_restriction failed")

    def snapshot_trade_restricted(self) -> Dict[str, dt.datetime]:
        try:
            with self._lock:
                return dict(self._trade_restricted)
        except Exception:
            return {}

    # ========================================================
    # CLEAR
    # ========================================================

    def clear(self):
        try:
            with self._lock:
                self.five_sec_bars.clear()
                self.symbol_health.clear()
                self._trade_restricted.clear()
        except Exception:
            logger.exception("MonitorState.clear failed")


__all__ = [
    "MonitorState",
]