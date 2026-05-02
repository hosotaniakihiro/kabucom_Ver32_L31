# ============================================================
# File   : trading/risk/kill_switch.py
# Version: V63-GLOBAL-KILL-SWITCH
# ------------------------------------------------------------
# ✔ 全ポジ即時強制決済
# ✔ 日次ハードストップ連動
# ✔ collapse暴走連動
# ✔ Thread-safe
# ✔ 冪等設計
# ============================================================

from __future__ import annotations
import logging
from threading import Lock

from core.global_context.context import global_context as GC
from trading.exit.executor import execute_exit

logger = logging.getLogger(__name__)


class KillSwitch:

    def __init__(self):
        self._active = False
        self._lock = Lock()

    # --------------------------------------------------------

    def activate(self, reason: str):
        with self._lock:
            if self._active:
                return
            self._active = True

        logger.critical("[KILL_SWITCH] ACTIVATED reason=%s", reason)
        self._force_liquidation(reason)

    # --------------------------------------------------------

    def _force_liquidation(self, reason: str):

        try:
            positions = {}

            if hasattr(GC.positions, "snapshot_open"):
                positions = GC.positions.snapshot_open()
            elif hasattr(GC.positions, "open_positions"):
                positions = dict(GC.positions.open_positions)

            for symbol, pos in positions.items():
                try:
                    price = None

                    bar = GC.monitor.get_five_sec_bar(symbol)
                    if bar:
                        price = bar.get("close")

                    if not price:
                        tick = GC.push.get_tick(symbol)
                        if tick:
                            price = tick.get("price")

                    if not price:
                        continue

                    execute_exit(symbol, price, f"KILL_SWITCH_{reason}")

                except Exception:
                    logger.exception("[KILL_SWITCH_EXIT_ERROR] %s", symbol)

        except Exception:
            logger.exception("[KILL_SWITCH_FATAL]")

    # --------------------------------------------------------

    def is_active(self):
        with self._lock:
            return self._active

    # --------------------------------------------------------

    def reset(self):
        with self._lock:
            self._active = False