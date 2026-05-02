# ============================================================
# File   : AI/exit_bandit.py
# Version: V31-FINAL-EXIT-BANDIT-CONTEXTUAL
# ------------------------------------------------------------
# ✔ cluster × regime × inago_state 対応
# ✔ Thompson Sampling
# ✔ 初期状態自動生成
# ✔ 例外安全
# ✔ None安全
# ✔ last_updated対応
# ✔ 将来RL拡張可能構造
# ============================================================

from __future__ import annotations

import numpy as np
import datetime as dt
import logging
from typing import Dict

from sqlalchemy.orm import Session
from database.bandit_models import ExitBanditState

logger = logging.getLogger(__name__)


# ============================================================
# ExitBandit
# ============================================================

class ExitBandit:

    """
    Contextual Thompson Sampling

    Key:
        cluster_id
        regime
        inago_state

    inago_state:
        0 = NONE
        1 = IGNITE
        2 = EXHAUST
    """

    def __init__(self, engine):
        self.engine = engine

    # ========================================================
    # Internal: get or create state
    # ========================================================

    def _get_state(
        self,
        cluster_id: int,
        regime: int,
        inago_state: int
    ) -> ExitBanditState:

        try:
            with Session(self.engine) as s:

                obj = s.get(
                    ExitBanditState,
                    {
                        "cluster_id": cluster_id,
                        "regime": regime,
                        "inago_state": inago_state,
                    }
                )

                if not obj:
                    obj = ExitBanditState(
                        cluster_id=cluster_id,
                        regime=regime,
                        inago_state=inago_state,
                        alpha=2.0,
                        beta=2.0,
                        last_updated=dt.datetime.utcnow(),
                    )
                    s.add(obj)
                    s.commit()

                return obj

        except Exception:
            logger.exception("ExitBandit._get_state failed")
            raise

    # ========================================================
    # Thompson Sampling
    # ========================================================

    @staticmethod
    def sample(alpha: float, beta: float) -> float:
        try:
            return float(np.random.beta(alpha, beta))
        except Exception:
            return 0.5

    # ========================================================
    # Get weights
    # ========================================================

    def get_weights(
        self,
        cluster_id: int,
        regime: int,
        inago_state: int = 0
    ) -> Dict[str, float]:

        try:
            obj = self._get_state(cluster_id, regime, inago_state)

            sampled = self.sample(obj.alpha, obj.beta)

            # collapse重みは確率で可変
            w_collapse = 0.4 + sampled * 0.6

            # inago状態による調整
            if inago_state == 1:      # IGNITE
                w_hold = 0.6
                w_take = 0.4
            elif inago_state == 2:    # EXHAUST
                w_hold = 0.2
                w_take = 0.8
            else:                     # NONE
                w_hold = 0.4
                w_take = 0.5

            return {
                "w_collapse": float(w_collapse),
                "w_take": float(w_take),
                "w_hold": float(w_hold),
            }

        except Exception:
            logger.exception("ExitBandit.get_weights failed")

            # fallback（安全側）
            return {
                "w_collapse": 0.6,
                "w_take": 0.5,
                "w_hold": 0.4,
            }

    # ========================================================
    # Update reward
    # ========================================================

    def update(
        self,
        cluster_id: int,
        regime: int,
        inago_state: int,
        reward: float
    ):

        """
        reward > 0 → 成功 → alpha++
        reward <= 0 → 失敗 → beta++
        """

        try:
            with Session(self.engine) as s:

                obj = s.get(
                    ExitBanditState,
                    {
                        "cluster_id": cluster_id,
                        "regime": regime,
                        "inago_state": inago_state,
                    }
                )

                if not obj:
                    obj = ExitBanditState(
                        cluster_id=cluster_id,
                        regime=regime,
                        inago_state=inago_state,
                        alpha=2.0,
                        beta=2.0,
                        last_updated=dt.datetime.utcnow(),
                    )
                    s.add(obj)

                if reward is None:
                    reward = 0.0

                if reward > 0:
                    obj.alpha += 1.0
                else:
                    obj.beta += 1.0

                obj.last_updated = dt.datetime.utcnow()

                s.commit()

        except Exception:
            logger.exception("ExitBandit.update failed")