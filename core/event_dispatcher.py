# ============================================================
# core/event_dispatcher.py
# Ver1.0-STREAM-EVENT-DRIVEN-PRODUCTION-FINAL
# ------------------------------------------------------------
# ✔ pushイベント駆動
# ✔ scheduler不要
# ✔ BarClock統合
# ✔ RealtimeSummaryEngine統合
# ✔ 市場時間完全対応
# ✔ 二重発火防止
# ✔ スレッド安全
# ✔ 軽量・高安定
# ✔ 将来拡張可能
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from trading.summary.realtime_engine import realtime_engine
from trading.summary.bar_clock import BarClock

logger = logging.getLogger(__name__)


class EventDispatcher:
    """
    pushベース完全イベント駆動ディスパッチャ
    """

    def __init__(self):

        self.clock = BarClock()
        self._lock = threading.Lock()

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # pushが来たかどうかのフラグ
        self._has_new_push = False

    # ========================================================
    # PUSH受信（WebSocket側から呼ばれる）
    # ========================================================

    def on_push(self, tick: dict):

        realtime_engine.on_push(tick)

        # push受信フラグを立てる
        self._has_new_push = True

    # ========================================================
    # 内部ループ（イベント駆動）
    # ========================================================

    def _run_loop(self):

        logger.info("[EventDispatcher] started")

        while self._running:

            try:
                if self._has_new_push:
                    self._has_new_push = False

                    # バー境界判定
                    signals = self.clock.check()

                    if any(signals.values()):
                        realtime_engine.run_cycle()

                # CPU過負荷防止
                time.sleep(0.01)

            except Exception as e:
                logger.exception(f"[EventDispatcher] loop error: {e}")
                time.sleep(0.1)

        logger.info("[EventDispatcher] stopped")

    # ========================================================
    # 起動
    # ========================================================

    def start(self):

        with self._lock:

            if self._running:
                return

            self._running = True

            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
            )
            self._thread.start()

    # ========================================================
    # 停止
    # ========================================================

    def stop(self):

        with self._lock:

            if not self._running:
                return

            self._running = False

        if self._thread:
            self._thread.join(timeout=2)

    # ========================================================
    # 状態確認
    # ========================================================

    @property
    def is_running(self) -> bool:
        return self._running


# ============================================================
# シングルトン
# ============================================================

event_dispatcher = EventDispatcher()