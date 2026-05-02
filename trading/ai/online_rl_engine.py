# ============================================================
# File   : trading/ai/online_rl_engine.py
# Version: FINAL-ONLINE-RL-V1
# ------------------------------------------------------------
# ✔ 疑似オンライン強化学習
# ✔ 攻撃度自動調整
# ✔ PF監視型
# ✔ 上下限制御
# ============================================================

class OnlineAggressionRL:

    def __init__(self, base_aggression=1.0):
        self.aggression = base_aggression
        self.learning_rate = 0.02
        self.min_aggr = 0.5
        self.max_aggr = 3.0

    def update(self, pnl, drawdown_penalty=0.0):

        reward = pnl - drawdown_penalty

        self.aggression += self.learning_rate * reward

        self.aggression = max(
            self.min_aggr,
            min(self.aggression, self.max_aggr)
        )

    def get(self):
        return self.aggression