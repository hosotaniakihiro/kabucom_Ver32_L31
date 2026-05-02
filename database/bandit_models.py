# ============================================================
# File   : database/bandit_models.py
# Version: V31-FINAL-EXIT-BANDIT-MODEL
# ------------------------------------------------------------
# ✔ cluster × regime × inago_state 複合主キー
# ✔ Thompson Sampling 用 alpha / beta
# ✔ last_updated 管理
# ✔ 将来拡張可能設計
# ✔ ADD ONLY思想
# ============================================================

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Column,
    Integer,
    Float,
    DateTime,
    Index,
)
from sqlalchemy.orm import declarative_base

BaseBandit = declarative_base()


# ============================================================
# ExitBanditState
# ============================================================

class ExitBanditState(BaseBandit):
    """
    Exit Thompson Sampling 状態保存テーブル

    主キー:
        cluster_id
        regime
        inago_state

    inago_state:
        0 = NONE
        1 = IGNITE
        2 = EXHAUST
    """

    __tablename__ = "exit_bandit_state"

    # --------------------------------------------------------
    # Composite Primary Key
    # --------------------------------------------------------
    cluster_id = Column(Integer, primary_key=True)
    regime = Column(Integer, primary_key=True)
    inago_state = Column(Integer, primary_key=True)

    # --------------------------------------------------------
    # Thompson Parameters
    # --------------------------------------------------------
    alpha = Column(Float, nullable=False, default=2.0)
    beta = Column(Float, nullable=False, default=2.0)

    # --------------------------------------------------------
    # Meta
    # --------------------------------------------------------
    last_updated = Column(
        DateTime,
        nullable=False,
        default=dt.datetime.utcnow,
    )

    # --------------------------------------------------------
    # Optional future extension fields（将来用）
    # --------------------------------------------------------
    # success_count = Column(Integer, default=0)
    # failure_count = Column(Integer, default=0)

    # --------------------------------------------------------
    # Index（検索高速化）
    # --------------------------------------------------------
    __table_args__ = (
        Index(
            "idx_bandit_cluster_regime",
            "cluster_id",
            "regime",
        ),
    )

    # --------------------------------------------------------
    # Utility
    # --------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"<ExitBanditState("
            f"cluster={self.cluster_id}, "
            f"regime={self.regime}, "
            f"inago={self.inago_state}, "
            f"alpha={self.alpha:.2f}, "
            f"beta={self.beta:.2f}"
            f")>"
        )