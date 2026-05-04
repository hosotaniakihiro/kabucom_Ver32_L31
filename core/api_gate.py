# ============================================================
# core/api_gate.py
# ------------------------------------------------------------
# ✔ API 呼び出し集中管理
# ✔ 429 RateLimit 防止
# ============================================================

import time
import threading
import logging

logger = logging.getLogger(__name__)


class ApiGate:
    """
    API 呼び出しを1か所に集約し、
    最小インターバルを保証する
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_call = {}  # key -> timestamp

    def call(self, key: str, min_interval: float, func, *args, **kwargs):
        """
        key          : API種別名（positions / ats_register など）
        min_interval : 最小呼び出し間隔（秒）
        func         : 実際に呼びたい関数
        """
        with self._lock:
            now = time.time()
            last = self._last_call.get(key, 0)

            if now - last < min_interval:
                logger.debug(f"[ApiGate] skip {key}")
                return None

            self._last_call[key] = now

        return func(*args, **kwargs)


# グローバルで1個だけ使う
api_gate = ApiGate()
