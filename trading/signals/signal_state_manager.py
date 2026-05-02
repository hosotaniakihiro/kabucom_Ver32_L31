# ============================================================
# trading/signals/signal_state_manager.py
# Ver1.0-PRODUCTION-SIGNAL-STATE-MANAGER
# ------------------------------------------------------------
# ✔ 銘柄ごとのシグナル状態管理
# ✔ BUY / SHORT state
# ✔ 重複シグナル防止
# ✔ last signal time 管理
# ✔ メモリ安全
# ✔ 高速辞書構造
# ✔ signals_engine / pipeline 互換
# ============================================================

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


# ============================================================
# state manager
# ============================================================

class SignalStateManager:

    def __init__(self):

        # symbol -> state
        self._states = {}

    # ========================================================
    # get state
    # ========================================================

    def get_state(self, symbol: str):

        return self._states.get(symbol)

    # ========================================================
    # update state
    # ========================================================

    def update_state(
        self,
        symbol: str,
        buy_signals: list[str] | None = None,
        short_signals: list[str] | None = None,
        decision: str | None = None,
        timestamp=None
    ):

        try:

            if symbol not in self._states:

                self._states[symbol] = {
                    "buy_signals": [],
                    "short_signals": [],
                    "decision": None,
                    "last_update": None,
                    "last_buy": None,
                    "last_short": None,
                }

            state = self._states[symbol]

            if buy_signals is not None:

                state["buy_signals"] = buy_signals

                if buy_signals:

                    state["last_buy"] = timestamp or time.time()

            if short_signals is not None:

                state["short_signals"] = short_signals

                if short_signals:

                    state["last_short"] = timestamp or time.time()

            if decision is not None:

                state["decision"] = decision

            state["last_update"] = timestamp or time.time()

        except Exception:

            logger.exception(
                f"[SignalStateManager] update failed symbol={symbol}"
            )

    # ========================================================
    # get last decision
    # ========================================================

    def get_last_decision(self, symbol: str):

        state = self._states.get(symbol)

        if not state:
            return None

        return state.get("decision")

    # ========================================================
    # check duplicate signal
    # ========================================================

    def is_duplicate_signal(
        self,
        symbol: str,
        buy_signals: list[str],
        short_signals: list[str]
    ):

        state = self._states.get(symbol)

        if not state:
            return False

        prev_buy = state.get("buy_signals", [])
        prev_short = state.get("short_signals", [])

        if buy_signals == prev_buy and short_signals == prev_short:

            return True

        return False

    # ========================================================
    # reset symbol
    # ========================================================

    def reset_symbol(self, symbol: str):

        if symbol in self._states:

            del self._states[symbol]

    # ========================================================
    # reset all
    # ========================================================

    def reset_all(self):

        self._states.clear()

    # ========================================================
    # cleanup old states
    # ========================================================

    def cleanup(self, max_age_sec=3600):

        now = time.time()

        remove_keys = []

        for symbol, state in self._states.items():

            last = state.get("last_update")

            if last and now - last > max_age_sec:

                remove_keys.append(symbol)

        for symbol in remove_keys:

            del self._states[symbol]

        if remove_keys:

            logger.info(
                f"[SignalStateManager] cleaned {len(remove_keys)} states"
            )

    # ========================================================
    # get all states
    # ========================================================

    def get_all_states(self):

        return self._states

    # ========================================================
    # symbol count
    # ========================================================

    def size(self):

        return len(self._states)