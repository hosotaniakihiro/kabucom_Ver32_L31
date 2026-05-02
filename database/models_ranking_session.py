# ============================================================
# File   : database/models_ranking_session.py
# Version: Ver1.0.0-FINAL-RANKING-SESSION
# ------------------------------------------------------------
# ✔ ランキング「連続出現セッション」専用モデル
# ✔ 価格OHLC（ランキング価格）
# ✔ 順位推移・改善度・勢い
# ✔ summary乖離特徴量（MA / VWAP / close）
# ✔ ATS / entry_gate / AI 学習 用途
# ✔ SQLite / SQLAlchemy 完全対応
# ✔ migrate.py 同期前提
# ============================================================

import datetime as dt

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Index,
)

from database.bases import Base_ranking


class RankingSession1Min(Base_ranking):
    """
    ランキング連続出現セッション（1分粒度）

    1 session =
        symbol × ranking_type × 連続出現期間
    """

    __tablename__ = "ranking_session_1min"
    __table_args__ = {"extend_existing": True}

    # --------------------------------------------------------
    # PK
    # --------------------------------------------------------
    id = Column(Integer, primary_key=True, autoincrement=True)

    # --------------------------------------------------------
    # Identity
    # --------------------------------------------------------
    date = Column(String, index=True)          # YYYYMMDD
    symbol = Column(String, index=True)        # 銘柄コード
    ranking_type = Column(String, index=True)  # 売買代金 / 値上がり率 等
    session_id = Column(Integer)               # symbol×type内の連番

    # --------------------------------------------------------
    # Session Time
    # --------------------------------------------------------
    start_dt = Column(DateTime)  # 初回出現時刻
    end_dt = Column(DateTime)    # 最終出現時刻
    minutes = Column(Integer)    # 出現回数（=滞在分数）

    # --------------------------------------------------------
    # Rank statistics
    # --------------------------------------------------------
    rank_first = Column(Integer)  # 初回順位
    rank_last = Column(Integer)   # 最終順位
    rank_best = Column(Integer)   # 最良順位（最小）
    rank_worst = Column(Integer)  # 最悪順位（最大）

    # --------------------------------------------------------
    # Price (ranking price OHLC)
    # --------------------------------------------------------
    rank_open = Column(Float)   # 初回ランキング価格
    rank_close = Column(Float)  # 最終ランキング価格
    rank_high = Column(Float)   # セッション中最高値
    rank_low = Column(Float)    # セッション中最安値

    # --------------------------------------------------------
    # Derived metrics
    # --------------------------------------------------------
    rank_ret = Column(Float)        # close / open - 1
    rank_range = Column(Float)      # high / low - 1
    rank_improve = Column(Integer)  # rank_first - rank_best
    rank_slope = Column(Float)      # (rank_last - rank_first) / minutes

    # --------------------------------------------------------
    # Summary gap features
    # --------------------------------------------------------
    d_ma25 = Column(Float)    # rank_close / ma25 - 1
    d_ma75 = Column(Float)    # rank_close / ma75 - 1
    d_vwap = Column(Float)    # rank_close / vwap - 1
    d_close = Column(Float)   # rank_close / summary_close - 1

    # --------------------------------------------------------
    # Quality judgment
    # --------------------------------------------------------
    quality = Column(String)  # STRONG / WEAK / REJECT

    # --------------------------------------------------------
    # Meta
    # --------------------------------------------------------
    created_at = Column(
        DateTime,
        default=dt.datetime.now,
        nullable=False,
    )


# ------------------------------------------------------------
# Indexes
# ------------------------------------------------------------
Index(
    "idx_ranking_session_date_symbol_type",
    RankingSession1Min.date,
    RankingSession1Min.symbol,
    RankingSession1Min.ranking_type,
)

Index(
    "idx_ranking_session_quality",
    RankingSession1Min.date,
    RankingSession1Min.quality,
)