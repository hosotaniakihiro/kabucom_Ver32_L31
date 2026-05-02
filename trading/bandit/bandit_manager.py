# ============================================================
# File   : trading/bandit/bandit_manager.py
# Version: FINAL-ROBUST-BANDIT-MANAGER
# ------------------------------------------------------------
# ✔ regime × cluster 対応
# ✔ Thompson Sampling 使用
# ✔ thread-safe
# ✔ 永続化フック用意
# ✔ NaN / 例外耐性
# ============================================================

from __future__ import annotations
import logging
import threading
from typing import Dict
from trading.bandit.bandit_engine import ThompsonBandit

logger = logging.getLogger(__name__)


class BanditManager:
    """
    regime × cluster 単位で bandit を管理するマネージャ
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.bandits: Dict[str, ThompsonBandit] = {}

    # --------------------------------------------------------
    # key生成
    # --------------------------------------------------------
    def _key(self, regime: str, cluster: str) -> str:
        regime = regime or "neutral"
        cluster = cluster or "unknown"
        return f"{regime}__{cluster}"

    # --------------------------------------------------------
    # bandit取得
    # --------------------------------------------------------
    def _get_bandit(self, regime: str, cluster: str) -> ThompsonBandit:
        key = self._key(regime, cluster)

        with self._lock:
            if key not in self.bandits:
                self.bandits[key] = ThompsonBandit()
                logger.info("[BANDIT_MANAGER] new arm created: %s", key)

            return self.bandits[key]

    # --------------------------------------------------------
    # 重み取得
    # --------------------------------------------------------
    def get_weight(self, regime: str, cluster: str) -> float:
        try:
            bandit = self._get_bandit(regime, cluster)
            return bandit.get_weight(self._key(regime, cluster))
        except Exception:
            logger.exception("[BANDIT_MANAGER] get_weight failed")
            return 1.0

    # --------------------------------------------------------
    # reward更新
    # --------------------------------------------------------
    def update(self, regime: str, cluster: str, reward: float):
        try:
            bandit = self._get_bandit(regime, cluster)
            bandit.update(self._key(regime, cluster), reward)
        except Exception:
            logger.exception("[BANDIT_MANAGER] update failed")

    # --------------------------------------------------------
    # 全weight取得
    # --------------------------------------------------------
    def get_all_weights(self):
        result = {}
        with self._lock:
            for key, bandit in self.bandits.items():
                result[key] = bandit.get_weight(key)
        return result