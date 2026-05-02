# ============================================================
# File   : trading/exit/meta_exit_controller.py
# Version: V45-FINAL-META-EXIT-RL-READY
# ------------------------------------------------------------
# ✔ collapse最優先防御
# ✔ inago統合
# ✔ regime統合
# ✔ bandit重み統合
# ✔ RL共存設計
# ✔ long/short両対応
# ✔ スコア合成強化
# ✔ 例外安全
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


# ============================================================
# MetaExitController
# ============================================================

class MetaExitController:

    """
    役割:
        - collapse / inago / bandit / regime を統合
        - 最終EXIT判断を行う
        - RLと共存可能な設計

    return:
        None
        "COLLAPSE_EXIT"
        "INAGO_EXHAUST_EXIT"
        "AI_EXIT"
        "TAKE_PROFIT"
        "TRAIL_EXIT"
    """

    def __init__(
        self,
        regime_model,
        collapse_model,
        inago_model,
        bandit,
        rl_agent=None,
    ):
        self.regime_model = regime_model
        self.collapse_model = collapse_model
        self.inago_model = inago_model
        self.bandit = bandit
        self.rl_agent = rl_agent

    # ========================================================
    # PUBLIC API
    # ========================================================

    def decide(
        self,
        position,
        features: Dict[str, Any],
        market_state: Dict[str, Any],
    ) -> Optional[str]:

        try:
            # ------------------------------------------------
            # 1️⃣ regime
            # ------------------------------------------------
            regime = self._safe_regime(market_state)

            # ------------------------------------------------
            # 2️⃣ collapse
            # ------------------------------------------------
            collapse_prob = self._safe_collapse(position, features)

            # collapse最優先防御
            if collapse_prob > 0.85:
                return "COLLAPSE_EXIT"

            # ------------------------------------------------
            # 3️⃣ inago
            # ------------------------------------------------
            inago_state = self._safe_inago(features)

            # exhaust即撤退
            if inago_state == 2:
                return "INAGO_EXHAUST_EXIT"

            # ------------------------------------------------
            # 4️⃣ bandit重み
            # ------------------------------------------------
            cluster_id = getattr(position, "cluster_id", 0) or 0

            weights = self._safe_bandit(
                cluster_id,
                regime,
                inago_state,
            )

            # ------------------------------------------------
            # 5️⃣ スコア合成
            # ------------------------------------------------
            hold_score = self._safe_float(features.get("hold_score"))
            take_score = self._safe_float(features.get("takeprofit_score"))

            score = (
                weights["w_collapse"] * collapse_prob
                + weights["w_take"] * take_score
                - weights["w_hold"] * hold_score
            )

            # ------------------------------------------------
            # 6️⃣ RL統合（任意）
            # ------------------------------------------------
            if self.rl_agent:

                try:
                    pnl = self._safe_float(features.get("unrealized_pnl"))
                    state = self.rl_agent.encode_state(
                        regime,
                        cluster_id,
                        inago_state,
                        pnl,
                    )

                    action = self.rl_agent.select_action(state)

                    if action == "EXIT":
                        return "RL_EXIT"

                    if action == "TAKE":
                        return "TAKE_PROFIT"

                    # HOLDなら通常ロジックへ

                except Exception:
                    logger.exception("RL decision failed")

            # ------------------------------------------------
            # 7️⃣ AI判定
            # ------------------------------------------------
            if score > 0.70:
                return "AI_EXIT"

            # ------------------------------------------------
            # 8️⃣ trail fallback
            # ------------------------------------------------
            if features.get("trail_hit"):
                return "TRAIL_EXIT"

            return None

        except Exception:
            logger.exception("MetaExitController.decide failed")
            return None

    # ========================================================
    # SAFE WRAPPERS
    # ========================================================

    def _safe_regime(self, market_state: dict) -> int:
        try:
            return self.regime_model.predict(market_state)
        except Exception:
            logger.exception("regime predict failed")
            return 2  # RANGE fallback

    def _safe_collapse(self, position, features: dict) -> float:
        try:
            return float(
                self.collapse_model.predict_proba(
                    features,
                    side=getattr(position, "side", "BUY"),
                )
            )
        except Exception:
            logger.exception("collapse predict failed")
            return 0.0

    def _safe_inago(self, features: dict) -> int:
        try:
            ignite_prob, exhaust_prob = self.inago_model.predict(features)

            if ignite_prob > 0.7:
                return 1
            if exhaust_prob > 0.6:
                return 2
            return 0

        except Exception:
            logger.exception("inago predict failed")
            return 0

    def _safe_bandit(self, cluster_id: int, regime: int, inago_state: int):
        try:
            return self.bandit.get_weights(
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
            )
        except Exception:
            logger.exception("bandit get_weights failed")
            return {
                "w_collapse": 0.6,
                "w_hold": 0.4,
                "w_take": 0.5,
            }

    def _safe_float(self, value):
        try:
            if value is None:
                return 0.0
            return float(value)
        except Exception:
            return 0.0