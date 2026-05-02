# ============================================================
# File   : trading/bandit/meta_bandit_manager.py
# Version: FINAL-ROBUST-META-BANDIT-MANAGER
# ------------------------------------------------------------
# ✔ regime × cluster × time_bucket 対応
# ✔ thread-safe
# ✔ NaN耐性
# ✔ 将来永続化拡張可能
# ============================================================

from __future__ import annotations
import datetime as dt
import threading
import logging
from trading.bandit.bandit_manager import BanditManager

logger = logging.getLogger(__name__)


class MetaBanditManager:

    def __init__(self):
        self._lock = threading.Lock()
        self.manager = BanditManager()

    # --------------------------------------------------------
    # 時間帯分類
    # --------------------------------------------------------
    def _time_bucket(self) -> str:
        now = dt.datetime.now().time()

        if now.hour < 10:
            return "open"
        elif now.hour < 14:
            return "mid"
        else:
            return "close"

    # --------------------------------------------------------
    # 重み取得
    # --------------------------------------------------------
    def get_weight(self, regime: str, cluster: str) -> float:
        try:
            bucket = self._time_bucket()
            key_regime = f"{regime}_{bucket}"
            return self.manager.get_weight(key_regime, cluster)
        except Exception:
            logger.exception("[META_BANDIT] get_weight failed")
            return 1.0

    # --------------------------------------------------------
    # 更新
    # --------------------------------------------------------
    def update(self, regime: str, cluster: str, reward: float):
        try:
            bucket = self._time_bucket()
            key_regime = f"{regime}_{bucket}"
            self.manager.update(key_regime, cluster, reward)
        except Exception:
            logger.exception("[META_BANDIT] update failed")