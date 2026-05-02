# ============================================================
# File   : trading/monitor/boost_monitor.py
# Version: Ver27-PRODUCTION-BOOST-MONITOR-FINAL
# ------------------------------------------------------------
# ✔ Boost状態監視
# ✔ 発動・解除ログ
# ✔ 継続時間計測
# ✔ 指標可視化
# ✔ Slack通知拡張可能
# ✔ Prometheus拡張可能
# ✔ 本番例外耐性
# ============================================================

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class BoostMonitor:

    def __init__(self):
        self._last_state: bool = False
        self._activated_time: Optional[float] = None

    # --------------------------------------------------------
    # 状態更新
    # --------------------------------------------------------

    def update(
        self,
        active: bool,
        win_rate: float,
        drawdown: float,
        collapse_prob: float,
        regime: int,
    ):
        """
        Boost状態を監視・ログ出力
        """

        try:
            now = time.time()

            # ------------------------------
            # 状態変化検知
            # ------------------------------
            if active and not self._last_state:
                self._activated_time = now
                logger.info(
                    "🚀 BOOST MODE ON | win_rate=%.2f dd=%.2f collapse=%.2f regime=%s",
                    win_rate,
                    drawdown,
                    collapse_prob,
                    regime,
                )

            elif not active and self._last_state:
                duration = 0.0
                if self._activated_time:
                    duration = now - self._activated_time

                logger.info(
                    "🛑 BOOST MODE OFF | duration=%.1fs",
                    duration,
                )
                self._activated_time = None

            # ------------------------------
            # 継続中ログ（軽量）
            # ------------------------------
            if active:
                duration = 0.0
                if self._activated_time:
                    duration = now - self._activated_time

                logger.debug(
                    "[BOOST] active | duration=%.1fs win=%.2f dd=%.2f collapse=%.2f regime=%s",
                    duration,
                    win_rate,
                    drawdown,
                    collapse_prob,
                    regime,
                )

            self._last_state = active

        except Exception:
            logger.exception("[BOOST_MONITOR_ERROR]")

    # --------------------------------------------------------
    # 状態取得
    # --------------------------------------------------------

    def is_active(self) -> bool:
        return self._last_state

    def get_active_duration(self) -> float:
        if self._activated_time:
            return time.time() - self._activated_time
        return 0.0

    # --------------------------------------------------------
    # 外部通知（拡張用）
    # --------------------------------------------------------

    def notify_slack(self, webhook_url: str, message: str):
        """
        Slack通知（オプション）
        """
        try:
            import requests

            requests.post(webhook_url, json={"text": message}, timeout=3)
        except Exception:
            logger.exception("[BOOST_SLACK_NOTIFY_ERROR]")

    # --------------------------------------------------------
    # Prometheus拡張用（将来）
    # --------------------------------------------------------

    def get_metrics(self) -> dict:
        return {
            "boost_active": int(self._last_state),
            "boost_duration": self.get_active_duration(),
        }