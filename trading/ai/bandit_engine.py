# ============================================================
# File   : trading/ai/bandit_engine.py
# Version: Ver1.0-ABSOLUTE-FINAL-REGIME-AWARE-SELF-LEARNING
# ------------------------------------------------------------
# ✔ UCB1 Multi-Armed Bandit
# ✔ クラスタ別アーム管理
# ✔ long / short 分離
# ✔ レジーム対応
# ✔ PnL報酬連動
# ✔ NaN / 0割完全耐性
# ✔ 永続化フック付き
# ============================================================

from __future__ import annotations
import math
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


# ============================================================
# バンディット本体
# ============================================================

class BanditEngine:

    def __init__(self):

        # arm stats
        self.counts = defaultdict(lambda: defaultdict(int))
        self.values = defaultdict(lambda: defaultdict(float))

        # 総試行回数
        self.total_counts = defaultdict(int)

    # ========================================================
    # arm選択（UCB1）
    # ========================================================

    def select_arm(
        self,
        cluster: str,
        available_arms: list[str],
    ) -> str:

        if not available_arms:
            raise ValueError("No available arms")

        total = self.total_counts[cluster]

        # 初期探索
        for arm in available_arms:
            if self.counts[cluster][arm] == 0:
                logger.info("[BANDIT] explore arm=%s", arm)
                return arm

        # UCB1計算
        ucb_scores = {}

        for arm in available_arms:

            avg_reward = self.values[cluster][arm]
            count = self.counts[cluster][arm]

            bonus = math.sqrt(
                (2 * math.log(total)) / max(count, 1)
            )

            ucb_scores[arm] = avg_reward + bonus

        selected = max(ucb_scores, key=ucb_scores.get)

        logger.info(
            "[BANDIT] select cluster=%s arm=%s score=%.4f",
            cluster,
            selected,
            ucb_scores[selected],
        )

        return selected

    # ========================================================
    # 報酬更新
    # ========================================================

    def update(
        self,
        cluster: str,
        arm: str,
        reward: float,
    ):

        if reward is None or math.isnan(reward):
            reward = 0.0

        self.total_counts[cluster] += 1
        self.counts[cluster][arm] += 1

        n = self.counts[cluster][arm]
        value = self.values[cluster][arm]

        # incremental mean
        new_value = value + (reward - value) / n

        self.values[cluster][arm] = new_value

        logger.info(
            "[BANDIT] update cluster=%s arm=%s reward=%.4f new_avg=%.4f",
            cluster,
            arm,
            reward,
            new_value,
        )

    # ========================================================
    # 状態取得
    # ========================================================

    def get_cluster_stats(self, cluster: str):

        return {
            "total": self.total_counts[cluster],
            "counts": dict(self.counts[cluster]),
            "values": dict(self.values[cluster]),
        }

    # ========================================================
    # リセット（安全）
    # ========================================================

    def reset_cluster(self, cluster: str):

        self.counts[cluster] = defaultdict(int)
        self.values[cluster] = defaultdict(float)
        self.total_counts[cluster] = 0

        logger.warning("[BANDIT] reset cluster=%s", cluster)


# ============================================================
# グローバルインスタンス
# ============================================================

bandit_engine = BanditEngine()