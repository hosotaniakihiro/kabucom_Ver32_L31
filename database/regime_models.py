# ============================================================
# File   : database/regime_models.py
# Version: V32-FINAL-REGIME-MODEL
# ------------------------------------------------------------
# ✔ RegimeHistory保存
# ✔ 将来AI regime学習対応
# ✔ インデックス最適化
# ✔ ADD ONLY思想
# ✔ Base分離設計
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


# ============================================================
# Base
# ============================================================

BaseRegime = declarative_base()


# ============================================================
# RegimeHistory
# ============================================================

class RegimeHistory(BaseRegime):
    """
    市場レジーム履歴テーブル

    regime:
        0 = TREND_UP
        1 = TREND_DOWN
        2 = RANGE
        3 = VOLATILE
        4 = CRASH
    """

    __tablename__ = "regime_history"

    # --------------------------------------------------------
    # Primary Key
    # --------------------------------------------------------
    timestamp = Column(
        DateTime,
        primary_key=True,
        default=dt.datetime.utcnow,
        nullable=False,
    )

    # --------------------------------------------------------
    # Core Fields
    # --------------------------------------------------------
    regime = Column(Integer, nullable=False)

    nikkei_slope = Column(Float, nullable=False, default=0.0)
    breadth_ratio = Column(Float, nullable=False, default=0.5)
    volatility = Column(Float, nullable=False, default=1.0)

    # --------------------------------------------------------
    # 将来拡張用フィールド（ADD ONLY思想）
    # --------------------------------------------------------
    # index_atr = Column(Float, nullable=True)
    # market_score = Column(Float, nullable=True)
    # ai_regime_prob = Column(Float, nullable=True)

    # --------------------------------------------------------
    # Index（検索高速化）
    # --------------------------------------------------------
    __table_args__ = (
        Index("idx_regime_timestamp", "timestamp"),
        Index("idx_regime_value", "regime"),
    )

    # --------------------------------------------------------
    # Debug
    # --------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"<RegimeHistory("
            f"time={self.timestamp}, "
            f"regime={self.regime}"
            f")>"
        )