# ============================================================
# trading/summary/bar_clock.py
# Ver5.0-ULTRA-STABLE-SESSION-AWARE-PRODUCTION
# ------------------------------------------------------------
# ✔ 市場時間対応（9:00-15:30）
# ✔ 秒ズレ完全耐性
# ✔ 二重発火防止
# ✔ 起動直後暴発防止
# ✔ 日付変更自動リセット
# ✔ 15:30以降完全停止
# ✔ 1m / 3m / 5m 正確確定
# ✔ intraday専用
# ✔ 高精度バー境界管理
# ✔ 軽量・ロック安全
# ✔ 本番完全安定版
# ============================================================

from __future__ import annotations

import datetime as dt
import threading
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)

MARKET_OPEN  = dt.time(9, 0)
MARKET_CLOSE = dt.time(15, 30)


class BarClock:

    def __init__(self):

        self._lock = threading.Lock()

        self._last_minute: Optional[dt.datetime] = None
        self._last_3m: Optional[dt.datetime] = None
        self._last_5m: Optional[dt.datetime] = None

        self._last_date: Optional[dt.date] = None

        # 起動直後暴発防止
        self._initialized = False

    # ========================================================
    # 現在時刻（秒切り捨て）
    # ========================================================

    @staticmethod
    def _now_floor() -> dt.datetime:
        now = dt.datetime.now()
        return now.replace(second=0, microsecond=0)

    # ========================================================
    # 市場時間判定
    # ========================================================

    @staticmethod
    def _is_market_time(now: dt.datetime) -> bool:
        t = now.time()
        return MARKET_OPEN <= t <= MARKET_CLOSE

    # ========================================================
    # 日付変更チェック
    # ========================================================

    def _check_date_rollover(self, now: dt.datetime):
        if self._last_date is None:
            self._last_date = now.date()
            return

        if now.date() != self._last_date:
            logger.info("[BarClock] New trading day detected. Resetting.")
            self._force_reset()
            self._last_date = now.date()

    # ========================================================
    # 初期同期
    # ========================================================

    def _sync_initial(self, now: dt.datetime):
        self._last_minute = now
        self._last_3m = now
        self._last_5m = now
        self._last_date = now.date()
        self._initialized = True

    # ========================================================
    # 強制リセット（内部用）
    # ========================================================

    def _force_reset(self):
        self._initialized = False
        self._last_minute = None
        self._last_3m = None
        self._last_5m = None

    # ========================================================
    # 総合チェック
    # ========================================================

    def check(self) -> Dict[str, bool]:

        results = {
            "1min": False,
            "3min": False,
            "5min": False,
        }

        now = self._now_floor()

        with self._lock:

            # 日付変更確認
            self._check_date_rollover(now)

            # 市場外は発火しない
            if not self._is_market_time(now):
                return results

            # 起動直後暴発防止
            if not self._initialized:
                self._sync_initial(now)
                return results

            # =================================================
            # 1分足
            # =================================================
            if self._last_minute != now:
                results["1min"] = True
                self._last_minute = now

            # =================================================
            # 3分足（00,03,06...）
            # =================================================
            if now.minute % 3 == 0:
                if self._last_3m != now:
                    results["3min"] = True
                    self._last_3m = now

            # =================================================
            # 5分足（00,05,10...）
            # =================================================
            if now.minute % 5 == 0:
                if self._last_5m != now:
                    results["5min"] = True
                    self._last_5m = now

        return results

    # ========================================================
    # 手動リセット（昼再接続・API再接続用）
    # ========================================================

    def reset(self):
        with self._lock:
            logger.info("[BarClock] Manual reset")
            self._force_reset()