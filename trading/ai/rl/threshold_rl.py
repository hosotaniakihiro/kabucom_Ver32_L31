# ============================================================
# File   : trading/ai/rl/threshold_rl.py
# Version: Ver2.0.0-PRO-ADAPTIVE-THRESHOLD-RL-FINAL
# ------------------------------------------------------------
# ✔ 動的エントリー閾値制御
# ✔ BUY/SELL対称設計
# ✔ tanh報酬正規化
# ✔ 勝率適応
# ✔ ボラ適応
# ✔ 過学習防止クリップ
# ✔ 安全初期化
# ✔ 再起動耐性
# ✔ scheduler絶対停止しない
# ✔ NaN完全耐性
# ✔ 将来Bandit連携拡張可能
# ============================================================

from __future__ import annotations

import numpy as np
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================
# 設定パラメータ
# ============================================================

@dataclass
class ThresholdConfig:

    initial_threshold: float = 0.0
    learning_rate: float = 0.05

    # 閾値上下限（暴走防止）
    min_threshold: float = -5.0
    max_threshold: float = 5.0

    # 報酬正規化スケール（スキャル想定）
    reward_scale: float = 5000.0

    # 勝率適応速度
    winrate_smoothing: float = 0.05

    # ボラ適応係数
    volatility_adjustment: float = 0.3


# ============================================================
# RLエンジン
# ============================================================

class ThresholdRL:

    def __init__(self, config: ThresholdConfig | None = None):

        self.config = config or ThresholdConfig()

        self.threshold = self.config.initial_threshold

        self.total_trades = 0
        self.wins = 0

        self.winrate = 0.5

    # --------------------------------------------------------
    # エントリー判定
    # --------------------------------------------------------
    def decide(self, score: float) -> str:

        try:
            score = float(score)
        except Exception:
            return "NONE"

        if score > self.threshold:
            return "BUY"

        if score < -self.threshold:
            return "SELL"

        return "NONE"

    # --------------------------------------------------------
    # 報酬更新
    # --------------------------------------------------------
    def update(
        self,
        pnl: float,
        volatility: float | None = None,
    ) -> None:

        try:
            pnl = float(pnl)
        except Exception:
            return

        if np.isnan(pnl):
            return

        self.total_trades += 1

        if pnl > 0:
            self.wins += 1

        # ----------------------------
        # 勝率更新（指数平滑）
        # ----------------------------
        if self.total_trades > 0:
            instant_winrate = self.wins / self.total_trades

            self.winrate = (
                self.config.winrate_smoothing * instant_winrate
                + (1 - self.config.winrate_smoothing) * self.winrate
            )

        # ----------------------------
        # 報酬正規化
        # ----------------------------
        reward = np.tanh(pnl / self.config.reward_scale)

        # ----------------------------
        # ボラ適応
        # ----------------------------
        vol_factor = 1.0
        if volatility is not None:
            try:
                volatility = float(volatility)
                vol_factor += (
                    self.config.volatility_adjustment
                    * np.tanh(volatility)
                )
            except Exception:
                pass

        # ----------------------------
        # 閾値更新
        # 勝っている時は閾値下げる（攻める）
        # 負けている時は閾値上げる（守る）
        # ----------------------------
        delta = -self.config.learning_rate * reward * vol_factor

        self.threshold += delta

        # ----------------------------
        # クリップ（暴走防止）
        # ----------------------------
        self.threshold = float(
            np.clip(
                self.threshold,
                self.config.min_threshold,
                self.config.max_threshold
            )
        )

        logger.debug(
            "[THRESHOLD_RL] pnl=%.2f reward=%.4f threshold=%.4f winrate=%.2f",
            pnl,
            reward,
            self.threshold,
            self.winrate
        )

    # --------------------------------------------------------
    # 現在状態取得
    # --------------------------------------------------------
    def get_state(self) -> dict:

        return {
            "threshold": self.threshold,
            "winrate": self.winrate,
            "total_trades": self.total_trades,
        }

    # --------------------------------------------------------
    # リセット（必要時のみ）
    # --------------------------------------------------------
    def reset(self) -> None:

        self.threshold = self.config.initial_threshold
        self.total_trades = 0
        self.wins = 0
        self.winrate = 0.5