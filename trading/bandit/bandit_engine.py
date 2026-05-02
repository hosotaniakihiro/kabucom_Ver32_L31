# ============================================================
# File   : trading/bandit/bandit_engine.py
# Version: FINAL-ROBUST-THOMPSON-BANDIT
# ------------------------------------------------------------
# ✔ Thompson Sampling
# ✔ 成功/失敗記録
# ✔ online update
# ✔ regime対応可能
# ✔ cluster対応可能
# ✔ NaN耐性
# ============================================================

from __future__ import annotations
import random
import logging

logger = logging.getLogger(__name__)


class ThompsonBandit:

    def __init__(self):
        self.arms = {}  # key: arm_name → {"alpha": , "beta": }

    # --------------------------------------------------------
    # arm初期化
    # --------------------------------------------------------
    def _ensure_arm(self, arm: str):
        if arm not in self.arms:
            self.arms[arm] = {"alpha": 1.0, "beta": 1.0}

    # --------------------------------------------------------
    # 重み取得
    # --------------------------------------------------------
    def get_weight(self, arm: str) -> float:
        self._ensure_arm(arm)
        a = self.arms[arm]["alpha"]
        b = self.arms[arm]["beta"]
        return random.betavariate(a, b)

    # --------------------------------------------------------
    # 報酬更新
    # --------------------------------------------------------
    def update(self, arm: str, reward: float):
        self._ensure_arm(arm)

        if reward > 0:
            self.arms[arm]["alpha"] += 1
        else:
            self.arms[arm]["beta"] += 1

        logger.info(
            "[BANDIT] arm=%s alpha=%.1f beta=%.1f",
            arm,
            self.arms[arm]["alpha"],
            self.arms[arm]["beta"],
        )

    # --------------------------------------------------------
    # 全arm取得
    # --------------------------------------------------------
    def get_all_weights(self) -> dict:
        weights = {}
        for arm in self.arms:
            weights[arm] = self.get_weight(arm)
        return weights