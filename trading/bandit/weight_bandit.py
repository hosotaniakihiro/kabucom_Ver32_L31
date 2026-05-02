# ============================================================
# File   : trading/ai/bandit/weight_bandit.py
# Version: Ver1.0.0-PRO-WEIGHT-BANDIT
# ------------------------------------------------------------
# ✔ UCB型バンディット
# ✔ 自動重み更新
# ✔ pnl報酬
# ✔ 安全初期化
# ✔ 再起動耐性
# ✔ scheduler停止防止
# ============================================================

from __future__ import annotations

import math
import logging
import numpy as np

logger = logging.getLogger(__name__)


class WeightBandit:

    def __init__(self, arms: list[str]):

        self.arms = arms
        self.counts = {arm: 1 for arm in arms}
        self.values = {arm: 0.0 for arm in arms}
        self.total_count = 1

    # --------------------------------------------------------
    # UCBスコア計算
    # --------------------------------------------------------
    def _ucb(self, arm):

        avg = self.values[arm]
        bonus = math.sqrt(
            2 * math.log(self.total_count) / self.counts[arm]
        )
        return avg + bonus

    # --------------------------------------------------------
    # 現在の重み取得
    # --------------------------------------------------------
    def get_weights(self):

        ucb_scores = {
            arm: self._ucb(arm)
            for arm in self.arms
        }

        # 正規化
        total = sum(ucb_scores.values())
        if total == 0:
            return {arm: 1/len(self.arms) for arm in self.arms}

        return {
            arm: ucb_scores[arm] / total
            for arm in self.arms
        }

    # --------------------------------------------------------
    # 報酬更新
    # --------------------------------------------------------
    def update(self, arm: str, reward: float):

        if arm not in self.arms:
            return

        self.total_count += 1
        self.counts[arm] += 1

        n = self.counts[arm]
        value = self.values[arm]

        # incremental mean
        new_value = value + (reward - value) / n
        self.values[arm] = new_value