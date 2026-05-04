from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Boolean, Index
)
from sqlalchemy.ext.declarative import declarative_base
import datetime as dt

Base = declarative_base()

# =========================================================
# ランキング 1分スナップショット（学習・ENTRY用）
# =========================================================
class RankingSnapshot1Min(Base):
    __tablename__ = "ranking_snapshot_1min"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True)
    symbolname = Column(String)
    rank_type = Column(String)          # 値上がり率 / 売買高急増 etc
    market = Column(String)             # ALL / TP / TS / TG

    current_price = Column(Float)
    trading_volume = Column(Float)
    volume_speed = Column(Float)        # ★ 主軸

    snapshot_time = Column(DateTime, default=dt.datetime.now)  # JST
    source = Column(String, default="ranking")

    __table_args__ = (
        Index("idx_rank_snap_symbol_time", "symbol", "snapshot_time"),
    )


# =========================================================
# 殿様イナゴ用 5秒スナップショット（PUSH非依存）
# =========================================================
class Tosama5SecSnapshot(Base):
    __tablename__ = "tosama_5sec_snapshot"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True)
    price = Column(Float)
    volume = Column(Float)

    fast_ret = Column(Float)     # 直近n本の瞬間リターン
    accel = Column(Float)        # 加速度（任意）

    snapshot_time = Column(DateTime, default=dt.datetime.now)  # JST
    source = Column(String, default="tosama")


# =========================================================
# 殿様イナゴ取引ログ（AI学習用）
# =========================================================
class TosamaTradeLog(Base):
    __tablename__ = "tosama_trade_log"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True)
    side = Column(String)            # BUY / SELL
    entry_price = Column(Float)
    exit_price = Column(Float)
    pnl = Column(Float)

    ai_confidence = Column(Float)    # ★ 学習主キー
    ai_model = Column(String)        # tosama_ai_v1 など

    hold_seconds = Column(Integer)
    created_at = Column(DateTime, default=dt.datetime.now)
