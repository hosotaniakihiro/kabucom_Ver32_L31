# ============================================================
# File   : AI/exit_bandit.py
# Version: V32-FINAL-EXIT-BANDIT-PATHS-UNIFIED
# ------------------------------------------------------------
# ✔ cluster × regime × inago_state 対応
# ✔ Thompson Sampling (Beta)
# ✔ database.bandit_models 再利用（モデル重複排除）
# ✔ config.paths 連携
# ✔ WAL / synchronous=NORMAL
# ✔ ADD ONLY思想
# ✔ 例外安全
# ✔ reward None安全
# ✔ 将来Contextual / RL拡張可能構造
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import numpy as np
from typing import Dict

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from config.paths import get_path, ensure_dirs
from database.bandit_models import BaseBandit, ExitBanditState

logger = logging.getLogger(__name__)


# ============================================================
# SQLite Engine (WAL対応)
# ============================================================

def _create_engine_sqlite():

    db_path = get_path("bandit_db")

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.close()

    return engine


# ============================================================
# ExitBandit
# ============================================================

class ExitBandit:
    """
    cluster × regime × inago_state ごとの
    Thompson Sampling 重み最適化

    inago_state:
        0 = NONE
        1 = IGNITE
        2 = EXHAUST
    """

    def __init__(self, engine=None):

        ensure_dirs()

        self.engine = engine or _create_engine_sqlite()

        # ADD ONLY思想
        BaseBandit.metadata.create_all(self.engine)

    # ========================================================
    # Public API
    # ========================================================

    def get_weights(
        self,
        cluster_id: int,
        regime: int,
        inago_state: int
    ) -> Dict[str, float]:

        try:
            state = self._get_or_create(
                cluster_id,
                regime,
                inago_state
            )

            sampled = self._sample(state.alpha, state.beta)

            # collapse重み
            w_collapse = 0.4 + sampled * 0.6

            # inago状態調整
            if inago_state == 1:      # IGNITE
                w_hold = 0.6
                w_take = 0.4
            elif inago_state == 2:    # EXHAUST
                w_hold = 0.2
                w_take = 0.8
            else:
                w_hold = 0.4
                w_take = 0.5

            return {
                "w_collapse": float(w_collapse),
                "w_hold": float(w_hold),
                "w_take": float(w_take),
            }

        except Exception:
            logger.exception("ExitBandit.get_weights failed")

            # 安全側フォールバック
            return {
                "w_collapse": 0.6,
                "w_hold": 0.4,
                "w_take": 0.5,
            }

    # ========================================================
    # Reward Update
    # ========================================================

    def update(
        self,
        cluster_id: int,
        regime: int,
        inago_state: int,
        reward: float
    ):
        """
        reward > 0 → alpha++
        reward <= 0 → beta++
        """

        try:
            with Session(self.engine) as session:

                state = session.get(
                    ExitBanditState,
                    {
                        "cluster_id": cluster_id,
                        "regime": regime,
                        "inago_state": inago_state,
                    }
                )

                if not state:
                    state = ExitBanditState(
                        cluster_id=cluster_id,
                        regime=regime,
                        inago_state=inago_state,
                        alpha=2.0,
                        beta=2.0,
                        last_updated=dt.datetime.utcnow(),
                    )
                    session.add(state)

                if reward is None:
                    reward = 0.0

                if reward > 0:
                    state.alpha += 1.0
                else:
                    state.beta += 1.0

                state.last_updated = dt.datetime.utcnow()

                session.commit()

        except Exception:
            logger.exception("ExitBandit.update failed")

    # ========================================================
    # Internal
    # ========================================================

    def _get_or_create(
        self,
        cluster_id: int,
        regime: int,
        inago_state: int
    ) -> ExitBanditState:

        with Session(self.engine) as session:

            state = session.get(
                ExitBanditState,
                {
                    "cluster_id": cluster_id,
                    "regime": regime,
                    "inago_state": inago_state,
                }
            )

            if not state:
                state = ExitBanditState(
                    cluster_id=cluster_id,
                    regime=regime,
                    inago_state=inago_state,
                    alpha=2.0,
                    beta=2.0,
                    last_updated=dt.datetime.utcnow(),
                )
                session.add(state)
                session.commit()

            return state

    @staticmethod
    def _sample(alpha: float, beta: float) -> float:
        try:
            return float(np.random.beta(alpha, beta))
        except Exception:
            return 0.5

    # ========================================================
    # Future Extension Hook
    # ========================================================

    def set_contextual_model(self, model):
        """
        将来ContextualBandit / RL拡張用
        """
        self._contextual_model = model