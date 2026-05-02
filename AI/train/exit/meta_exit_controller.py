# ============================================================
# File   : trading/exit/meta_exit_controller.py
# Version: V32-FINAL-META-EXIT-STABLE-EXTENSIBLE
# ------------------------------------------------------------
# ✔ collapse最優先防御
# ✔ inago統合（ignite / exhaust）
# ✔ bandit統合
# ✔ regime対応
# ✔ long/short両対応
# ✔ cluster安全取得
# ✔ 例外安全（絶対落ちない）
# ✔ 将来RL拡張フック
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# MetaExitController
# ============================================================

class MetaExitController:

    def __init__(
        self,
        regime_model,
        collapse_model,
        inago_model,
        bandit
    ):
        self.regime_model = regime_model
        self.collapse_model = collapse_model
        self.inago_model = inago_model
        self.bandit = bandit

    # ========================================================
    # Public API
    # ========================================================

    def decide(
        self,
        position,
        features: dict,
        market_state: dict
    ) -> Optional[str]:

        """
        return:
            None
            "COLLAPSE_EXIT"
            "INAGO_EXHAUST_EXIT"
            "AI_EXIT"
            "TRAIL_EXIT"
        """

        try:
            # ------------------------------------------------
            # 1️⃣ regime取得
            # ------------------------------------------------
            regime = self._safe_regime(market_state)

            # ------------------------------------------------
            # 2️⃣ collapse確率（最優先防御）
            # ------------------------------------------------
            collapse_prob = self._safe_collapse(
                features,
                getattr(position, "side", "LONG")
            )

            from AI.exit_models.collapse_model import collapse_decision

            if collapse_decision(collapse_prob, regime):
                return "COLLAPSE_EXIT"

            # ------------------------------------------------
            # 3️⃣ inago判定
            # ------------------------------------------------
            ignite_prob, exhaust_prob = self._safe_inago(features)

            if ignite_prob > 0.7:
                inago_state = 1
            elif exhaust_prob > 0.6:
                inago_state = 2
            else:
                inago_state = 0

            # exhaust即撤退
            if inago_state == 2:
                return "INAGO_EXHAUST_EXIT"

            # ------------------------------------------------
            # 4️⃣ bandit重み取得
            # ------------------------------------------------
            cluster_id = getattr(position, "cluster_id", 0)

            weights = self.bandit.get_weights(
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state
            )

            # ------------------------------------------------
            # 5️⃣ スコア合成
            # ------------------------------------------------
            hold_score = self._safe(features.get("hold_score"))
            takeprofit_score = self._safe(features.get("takeprofit_score"))

            score = (
                weights["w_collapse"] * collapse_prob
                + weights["w_take"] * takeprofit_score
                - weights["w_hold"] * hold_score
            )

            # ignite中はexit閾値を上げる（伸ばす）
            if inago_state == 1:
                threshold = 0.75
            else:
                threshold = 0.65

            # ------------------------------------------------
            # 6️⃣ AI判定
            # ------------------------------------------------
            if score > threshold:
                return "AI_EXIT"

            # ------------------------------------------------
            # 7️⃣ trail fallback
            # ------------------------------------------------
            if features.get("trail_hit"):
                return "TRAIL_EXIT"

            # ------------------------------------------------
            # 将来RL拡張フック
            # ------------------------------------------------
            # if hasattr(self, "_rl_model"):
            #     rl_decision = self._rl_model.predict(...)
            #     if rl_decision:
            #         return "RL_EXIT"

            return None

        except Exception:
            logger.exception("MetaExitController.decide failed")
            return None

    # ========================================================
    # Safe wrappers
    # ========================================================

    def _safe_regime(self, market_state: dict) -> int:
        try:
            return self.regime_model.predict(market_state)
        except Exception:
            return 2  # RANGE fallback

    def _safe_collapse(self, features: dict, side: str) -> float:
        try:
            return float(
                self.collapse_model.predict_proba(
                    features,
                    side=side
                )
            )
        except Exception:
            return 0.0

    def _safe_inago(self, features: dict):
        try:
            return self.inago_model.predict(features)
        except Exception:
            return 0.0, 0.0

    def _safe(self, value):
        try:
            if value is None:
                return 0.0
            return float(value)
        except Exception:
            return 0.0