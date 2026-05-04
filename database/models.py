# ============================================================
# File   : database/models.py
# Ver31.0.0-FINAL-MODELS-SUMMARY-WIDE-SCHEMA-AUTO-PATCH
# ------------------------------------------------------------
# ✔ Ver30.0.0 全機能保持
# ✔ Bollinger Band 正本：bb_mid / bb_upper / bb_lower / bb_width
# ✔ suffix(bb_*_1/3/5) 完全撤廃
# ✔ DB / ORM / summary_loader / migrate / crud / ws 完全整合
# ✔ multi-DB 構成厳守
# ✔ Position close_time 追加（kabu_api 完全互換）
# ✔ summary 保存列の広い schema に対応
# ✔ open/high/low/close, score系, mtf系, atr_* 系, ready系を追加
#
# 【Ver31.0.0 修正】
# ✔ summary_schema_patch.py を作らず models.py 内に統合
# ✔ StockSummaryBase に display_ready / mtf_score / mtf_alignment 等を追加
# ✔ score_base / score_trend / score_momentum / score_velocity 等の alias を追加
# ✔ direction_penalty / base_score / momentum_score 等も追加
# ✔ ensure_summary_wide_schema(engine) を追加
# ✔ 既存 stock_summary_1min / 3min / 5min に不足カラムを ALTER TABLE で追加可能
#
# 【重要】
#   SQLAlchemy の create_all() は既存テーブルへ不足カラムを追加しない。
#   既存DBに列を追加するには、起動時に以下を1回呼ぶこと:
#
#       from database.models import ensure_summary_wide_schema
#       ensure_summary_wide_schema(summary_engine)
#
#   呼び出し場所の候補:
#       database/session.py の init_engines() の summary_engine 作成直後
#       または core/startup/startup.py の summary DB 初期化直後
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    Time,
    UniqueConstraint,
    ForeignKey,
    Index,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.orm import declarative_base

from .bases import (
    Base_push,
    Base_summary,
    Base_position,
    Base_ranking,
)

try:
    from database.bases import Base_ai
except Exception:
    Base_ai = declarative_base()

Base = declarative_base()

logger = logging.getLogger(__name__)


# ============================================================
# SUMMARY WIDE SCHEMA PATCH
# ============================================================

SUMMARY_TABLE_NAMES = (
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
)

# SQLite 用。既存テーブルに無い場合だけ ALTER TABLE ADD COLUMN する。
# ORM定義と合わせる。
SUMMARY_WIDE_SCHEMA_COLUMNS: dict[str, str] = {
    # --------------------------------------------------------
    # OHLC aliases
    # --------------------------------------------------------
    "open": "REAL",
    "high": "REAL",
    "low": "REAL",
    "close": "REAL",
    "open_price": "REAL",
    "high_price": "REAL",
    "low_price": "REAL",
    "close_price": "REAL",

    # --------------------------------------------------------
    # basic / metadata
    # --------------------------------------------------------
    "source": "VARCHAR",
    "interval": "INTEGER",
    "last_update": "DATETIME",

    # --------------------------------------------------------
    # technical
    # --------------------------------------------------------
    "volume": "REAL",
    "vwap": "REAL",
    "ma5": "REAL",
    "ma25": "REAL",
    "ma75": "REAL",
    "ma5_conf": "REAL",
    "ma25_conf": "REAL",
    "ma75_conf": "REAL",
    "ma75_slope": "REAL",
    "volume_slope": "REAL",
    "vwap_slope": "REAL",
    "slope": "REAL",
    "slope_atr_scaled": "REAL",
    "slope_atr_scaled_1m": "REAL",
    "slope_atr_scaled_3m": "REAL",
    "slope_atr_scaled_5m": "REAL",
    "ema12": "REAL",
    "ema26": "REAL",
    "macd": "REAL",
    "signal": "REAL",
    "hist": "REAL",
    "rsi": "REAL",
    "rci": "REAL",
    "atr": "REAL",
    "atr_1m": "REAL",
    "atr_3m": "REAL",
    "atr_5m": "REAL",
    "bb_mid": "REAL",
    "bb_upper": "REAL",
    "bb_lower": "REAL",
    "bb_width": "REAL",

    # --------------------------------------------------------
    # score / display
    # --------------------------------------------------------
    "score": "REAL",
    "score_total": "REAL",
    "display_score": "REAL",
    "final_score": "REAL",
    "score_buy": "REAL",
    "score_sell": "REAL",
    "buy_score": "REAL",
    "sell_score": "REAL",
    "score_slope": "REAL",
    "score_mtf": "REAL",
    "mtf": "REAL",
    "mtf_alignment": "REAL",
    "mtf_score": "REAL",
    "price_diff": "REAL",
    "base": "REAL",
    "trend": "REAL",
    "mom": "REAL",
    "vel": "REAL",
    "pen": "REAL",
    "combined_score": "REAL",

    # scoring aliases / diagnostics
    "score_base": "REAL",
    "score_trend": "REAL",
    "score_momentum": "REAL",
    "score_velocity": "REAL",
    "direction_penalty": "REAL",
    "base_score": "REAL",
    "momentum_score": "REAL",
    "volume_score": "REAL",
    "flag_score": "REAL",
    "sell_pressure": "REAL",
    "absolute_score": "REAL",
    "liquidity_score": "REAL",
    "distribution_score": "REAL",
    "volatility_score": "REAL",
    "score_rank": "REAL",
    "ai_score": "REAL",

    # readiness / history
    "symbol_hist_len": "REAL",
    "technical_ready": "INTEGER",
    "display_ready": "INTEGER",
}


def _quote_ident(name: str) -> str:
    s = str(name).replace('"', '""')
    return f'"{s}"'


def _table_exists(conn, table_name: str) -> bool:
    try:
        row = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:name",
            {"name": table_name},
        ).fetchone()
        return row is not None
    except Exception:
        logger.debug("[models] table_exists failed table=%s", table_name, exc_info=True)
        return False


def _get_table_columns(conn, table_name: str) -> set[str]:
    try:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({_quote_ident(table_name)})").fetchall()
        return {
            str(r[1]).strip()
            for r in rows
            if len(r) > 1 and r[1] is not None and str(r[1]).strip()
        }
    except Exception:
        logger.debug("[models] get_table_columns failed table=%s", table_name, exc_info=True)
        return set()


def ensure_summary_wide_schema(engine: Any, *, intervals: tuple[int, ...] = (1, 3, 5)) -> dict[str, list[str]]:
    """
    既存 summary DB の stock_summary_* に不足カラムを追加する。

    Parameters
    ----------
    engine:
        SQLAlchemy Engine。
        database.session.summary_engine または get_summary_engine() の戻り値を渡す。

    intervals:
        対象 interval。通常は (1, 3, 5)。

    Returns
    -------
    dict[str, list[str]]
        table_name -> added columns

    Notes
    -----
    create_all() は既存テーブルにカラム追加しないため、
    起動時にこの関数を呼ぶ必要がある。
    """
    result: dict[str, list[str]] = {}

    if engine is None:
        logger.warning("[models] ensure_summary_wide_schema skipped: engine is None")
        return result

    try:
        with engine.begin() as conn:
            try:
                conn.exec_driver_sql("PRAGMA busy_timeout=30000")
            except Exception:
                pass

            for interval in intervals:
                table_name = f"stock_summary_{int(interval)}min"
                result[table_name] = []

                if not _table_exists(conn, table_name):
                    logger.warning("[models] summary table not found, skip patch table=%s", table_name)
                    continue

                existing = _get_table_columns(conn, table_name)
                if not existing:
                    logger.warning("[models] summary table columns empty, skip patch table=%s", table_name)
                    continue

                for col, col_type in SUMMARY_WIDE_SCHEMA_COLUMNS.items():
                    if col in existing:
                        continue

                    sql = (
                        f"ALTER TABLE {_quote_ident(table_name)} "
                        f"ADD COLUMN {_quote_ident(col)} {col_type}"
                    )

                    try:
                        conn.exec_driver_sql(sql)
                        result[table_name].append(col)
                        existing.add(col)
                    except Exception as e:
                        msg = str(e).lower()
                        if "duplicate column" in msg or "already exists" in msg:
                            existing.add(col)
                            continue

                        logger.warning(
                            "[models] add summary column failed table=%s col=%s type=%s err=%r",
                            table_name,
                            col,
                            col_type,
                            e,
                        )

                if result[table_name]:
                    logger.warning(
                        "[models] summary wide schema patched table=%s added=%s",
                        table_name,
                        result[table_name],
                    )
                else:
                    logger.info("[models] summary wide schema ok table=%s", table_name)

    except Exception:
        logger.exception("[models] ensure_summary_wide_schema failed")

    return result


# ============================================================
# PUSH: stream_data（FULL STREAM + OHLC COMPAT）
# ============================================================

class StreamData(Base_push):
    __tablename__ = "stream_data"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)

    symbol = Column(String, index=True, nullable=False)
    symbolname = Column(String)

    datetime = Column(DateTime, index=True, nullable=False)
    date = Column(Date, index=True)
    time = Column(Time)

    created_at = Column(DateTime, default=dt.datetime.utcnow)

    open_price = Column(Float)
    high_price = Column(Float)
    low_price = Column(Float)
    close_price = Column(Float)

    volume = Column(Float)
    turnover = Column(Float)
    vwap = Column(Float)

    best_bid = Column(Float)
    best_ask = Column(Float)
    spread = Column(Float)

    content = Column(String)

    price = Column(Float)
    trading_value = Column(Float)

    previousclose = Column(Float)
    previousclose_time = Column(String)

    high_price_time = Column(String)
    low_price_time = Column(String)

    opening_price = Column(Float)
    opening_price_time = Column(String)

    current_price_time = Column(String)

    bid_price = Column(Float)
    bid_qty = Column(Float)

    ask_price = Column(Float)
    ask_qty = Column(Float)


# ============================================================
# Summary Base（共通・抽象）
# ============================================================

class StockSummaryBase(Base_summary):
    __abstract__ = True

    id = Column(Integer, primary_key=True, autoincrement=True)

    symbol = Column(String, index=True, nullable=False)
    symbolname = Column(String)

    datetime = Column(DateTime, index=True, nullable=False)

    date = Column(Date, index=True, nullable=False)
    time_range = Column(String, index=True, nullable=False)

    start_time = Column(Time)
    end_time = Column(Time)
    time = Column(Time)

    source = Column(String, default="push", index=True)
    interval = Column(Integer)

    # --------------------------------------------------------
    # OHLC: 新旧両方保持
    # --------------------------------------------------------
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)

    open_price = Column(Float)
    high_price = Column(Float)
    low_price = Column(Float)
    close_price = Column(Float)

    volume = Column(Float)
    vwap = Column(Float)

    # --------------------------------------------------------
    # moving average / technical
    # --------------------------------------------------------
    ma5 = Column(Float)
    ma25 = Column(Float)
    ma75 = Column(Float)

    ma5_conf = Column(Float)
    ma25_conf = Column(Float)
    ma75_conf = Column(Float)

    ma75_slope = Column(Float)
    volume_slope = Column(Float)
    vwap_slope = Column(Float)

    slope = Column(Float)
    slope_atr_scaled = Column(Float)
    slope_atr_scaled_1m = Column(Float)
    slope_atr_scaled_3m = Column(Float)
    slope_atr_scaled_5m = Column(Float)

    ema12 = Column(Float)
    ema26 = Column(Float)
    macd = Column(Float)
    signal = Column(Float)
    hist = Column(Float)

    rsi = Column(Float)
    rci = Column(Float)

    atr = Column(Float)
    atr_1m = Column(Float)
    atr_3m = Column(Float)
    atr_5m = Column(Float)

    bb_mid = Column(Float)
    bb_upper = Column(Float)
    bb_lower = Column(Float)
    bb_width = Column(Float)

    # --------------------------------------------------------
    # score / display
    # --------------------------------------------------------
    score = Column(Float)
    score_total = Column(Float)
    display_score = Column(Float)
    final_score = Column(Float)

    score_buy = Column(Float)
    score_sell = Column(Float)
    buy_score = Column(Float)
    sell_score = Column(Float)

    score_slope = Column(Float)
    score_mtf = Column(Float)

    mtf = Column(Float)
    mtf_alignment = Column(Float)
    mtf_score = Column(Float)

    price_diff = Column(Float)

    base = Column(Float)
    trend = Column(Float)
    mom = Column(Float)
    vel = Column(Float)
    pen = Column(Float)

    combined_score = Column(Float)

    # scoring aliases / diagnostics
    score_base = Column(Float)
    score_trend = Column(Float)
    score_momentum = Column(Float)
    score_velocity = Column(Float)
    direction_penalty = Column(Float)
    base_score = Column(Float)
    momentum_score = Column(Float)
    volume_score = Column(Float)
    flag_score = Column(Float)
    sell_pressure = Column(Float)
    absolute_score = Column(Float)
    liquidity_score = Column(Float)
    distribution_score = Column(Float)
    volatility_score = Column(Float)
    score_rank = Column(Float)
    ai_score = Column(Float)

    # --------------------------------------------------------
    # readiness / history
    # --------------------------------------------------------
    symbol_hist_len = Column(Float)
    technical_ready = Column(Integer)
    display_ready = Column(Integer)

    last_update = Column(DateTime)


# ============================================================
# Summary 1min / 3min / 5min
# ============================================================

class StockSummary1Min(StockSummaryBase):
    __tablename__ = "stock_summary_1min"
    __table_args__ = (
        UniqueConstraint("symbol", "datetime"),
        {"extend_existing": True},
    )


class StockSummary3Min(StockSummaryBase):
    __tablename__ = "stock_summary_3min"
    __table_args__ = (
        UniqueConstraint("symbol", "datetime"),
        {"extend_existing": True},
    )


class StockSummary5Min(StockSummaryBase):
    __tablename__ = "stock_summary_5min"
    __table_args__ = (
        UniqueConstraint("symbol", "datetime"),
        {"extend_existing": True},
    )


Summary = StockSummary1Min


# ============================================================
# Ranking（LEGACY / CRUD互換）
# ============================================================

class Ranking(Base_ranking):
    __tablename__ = "ranking"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)

    type = Column(String, nullable=False)
    market = Column(String, nullable=False)
    no = Column(Integer)

    symbol = Column(String, index=True)
    symbolname = Column(String)

    categoryname = Column(String)

    current_price = Column(Float)
    current_price_time = Column(String)

    change_percentage = Column(Float)
    change_ratio = Column(Float)

    trading_volume = Column(Float)
    turnover = Column(Float)

    trend = Column(String)
    exchange_name = Column(String)

    average_ranking = Column(Float)

    inserted_at = Column(DateTime, default=dt.datetime.utcnow, index=True)


# ============================================================
# Ranking MA / Snapshot / RAW
# ============================================================

class RankingMA1Min(Base_ranking):
    __tablename__ = "ranking_ma_1min"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)

    symbol = Column(String, index=True)
    rank_type = Column(String)
    market = Column(String)

    ma_rank_position = Column(Float)
    ma_volume_speed = Column(Float)
    trend_score = Column(Float)

    snapshot_time = Column(DateTime)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class RankingSnapshot1Min(Base_ranking):
    __tablename__ = "ranking_snapshot_1min"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)

    symbol = Column(String, index=True, nullable=False)
    symbolname = Column(String)

    rank_type = Column(String)
    rank_type_id = Column(Integer)
    market = Column(String)
    rank_position = Column(Integer)

    current_price = Column(Float)
    trading_volume = Column(Float)
    volume_speed = Column(Float)

    rank_strength = Column(Float)
    rank_persistence = Column(Integer)
    rank_delta = Column(Integer)

    price_delta_1m = Column(Float)
    volume_delta_1m = Column(Float)
    minute_of_day = Column(Integer)

    snapshot_time = Column(String, index=True, nullable=False)

    # ranking summary bootstrap / loader 互換用
    # snapshot_time と同じ分足時刻を入れる想定
    datetime = Column(String, index=True)

    source = Column(String, default="ranking")


class RankingRaw1Min(Base_ranking):
    __tablename__ = "ranking_raw_1min"
    __table_args__ = {"extend_existing": True}

    symbol = Column(String, primary_key=True)
    snapshot_time = Column(String, primary_key=True)

    symbolname = Column(String)

    rank_type = Column(String)
    rank_type_id = Column(Integer)
    market = Column(String)
    rank_position = Column(Integer)

    current_price = Column(Float)
    trading_volume = Column(Float)
    trading_value = Column(Float)
    volume_speed = Column(Float)

    price_delta_1m = Column(Float)
    volume_delta_1m = Column(Float)

    minute_of_day = Column(Integer)
    source = Column(String, default="ranking")
    # 新系 / snapshot_writer / ranking summary 互換
    ranking_type = Column(String, index=True)
    rank = Column(Integer)
    category = Column(String)

    price = Column(Float)
    volume = Column(Float)
    turnover = Column(Float)
    change_rate = Column(Float)

    datetime = Column(String, index=True)
    created_at = Column(String)


# ============================================================
# Entry / Exit / Trade / Position
# ============================================================

class EntryLog(Base_position):
    __tablename__ = "entry_log"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String, index=True, nullable=False)
    symbol = Column(String, index=True, nullable=False)
    symbolname = Column(String)
    entry_time = Column(DateTime, nullable=False)
    time_bucket = Column(String, index=True)
    is_buy = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    qty = Column(Integer, nullable=False)
    entry_source = Column(String)
    trigger_type = Column(String)
    ranking_type = Column(String)
    ranking_strength = Column(Integer)
    volume_speed = Column(Float)
    volume_ratio = Column(Float)
    rank_price = Column(Float)
    best_ask = Column(Float)
    best_bid = Column(Float)
    spread = Column(Float)
    price_vs_rank = Column(Float)
    ma25_conf = Column(Float)
    ma75_conf = Column(Float)
    final_score = Column(Float)
    created_at = Column(DateTime, default=dt.datetime.utcnow, index=True)


class ExitLog(Base_position):
    __tablename__ = "exit_log"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String, index=True, nullable=False)
    symbol = Column(String, index=True, nullable=False)
    exit_time = Column(DateTime, nullable=False)
    exit_price = Column(Float, nullable=False)
    pnl = Column(Float, nullable=False)
    pnl_pct = Column(Float, nullable=False)
    holding_seconds = Column(Integer)
    exit_reason = Column(String)
    created_at = Column(DateTime, default=dt.datetime.utcnow, index=True)


class TradeExitStats(Base_position):
    __tablename__ = "trade_exit_stats"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String, index=True, nullable=False)
    symbol = Column(String, index=True, nullable=False)
    side = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    atr_1min = Column(Float)
    mfe = Column(Float)
    mae = Column(Float)
    mfe_pct = Column(Float)
    mae_pct = Column(Float)
    holding_seconds = Column(Integer)
    exit_reason = Column(String)
    index_shock = Column(Integer)
    is_valid = Column(Integer, default=1)
    created_at = Column(DateTime, default=dt.datetime.utcnow, index=True)


class TradeHistory(Base_position):
    __tablename__ = "trade_history"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String)
    symbolname = Column(String)
    action = Column(String)
    qty = Column(Integer)
    side = Column(String)
    price = Column(Float)
    pnl = Column(Float)
    realized_pnl = Column(Float)
    order_id = Column(String)
    trade_time = Column(DateTime)
    reason = Column(String)
    position_id = Column(Integer, ForeignKey("positions.id"))
    position = relationship("Position", back_populates="histories")


class Position(Base_position):
    __tablename__ = "positions"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)

    symbol = Column(String, nullable=False, index=True)
    symbolname = Column(String)

    side = Column(String, nullable=False)
    exchange = Column(Integer)

    execution_id = Column(String)
    hold_id = Column(String, index=True)

    qty = Column(Integer, nullable=False)
    avg_price = Column(Float, nullable=False)
    price = Column(Float)
    exit_price = Column(Float)

    margin_trade_type = Column(Integer)
    account_type = Column(Integer)

    status = Column(String, default="OPEN")

    entry_time = Column(DateTime, nullable=False)

    exit_time = Column(DateTime)
    closed_time = Column(DateTime)
    close_time = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=dt.datetime.utcnow,
        onupdate=dt.datetime.utcnow,
        nullable=False,
    )

    histories = relationship(
        "TradeHistory",
        back_populates="position",
        cascade="all, delete-orphan",
    )


# ============================================================
# Symbol / State
# ============================================================

class SymbolFlags(Base_position):
    __tablename__ = "symbol_flags"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, unique=True, index=True, nullable=False)
    symbolname = Column(String)
    buy = Column(Integer)
    sell = Column(Integer)
    ats_ok = Column(Integer, default=1)
    push_ok = Column(Integer, default=1)
    short_sellable = Column(Integer, default=0)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class WatchlistHistory(Base_position):
    __tablename__ = "watchlist_history"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, index=True)
    reason = Column(String)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class ConfidenceBias(Base_position):
    __tablename__ = "confidence_bias"
    symbol = Column(String, primary_key=True)
    bias = Column(Float, default=1.0, nullable=False)
    trade_count = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)
    __table_args__ = (Index("idx_confidence_bias_symbol", "symbol", unique=True),)


class SymbolStateLog(Base_position):
    __tablename__ = "symbol_state_log"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, index=True, nullable=False)
    prev_state = Column(String, nullable=False)
    new_state = Column(String, nullable=False)
    ranking_score = Column(Float)
    summary_count = Column(Integer)
    turnover = Column(Float)
    ai_score = Column(Float)
    created_at = Column(DateTime, default=dt.datetime.utcnow, index=True)


# ============================================================
# Ranking Entry Event
# ============================================================

class RankingEntryEvent(Base_position):
    __tablename__ = "ranking_entry_event"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)

    symbol = Column(String, index=True, nullable=False)
    symbolname = Column(String)

    event_time = Column(DateTime, nullable=False, index=True)
    interval = Column(Integer)

    side = Column(String)

    rank_type = Column(String)
    rank_position = Column(Integer)
    rank_strength = Column(Float)
    rank_persistence = Column(Integer)
    volume_speed = Column(Float)

    change_rate = Column(Float)

    close_price = Column(Float)
    volume = Column(Float)
    vwap = Column(Float)

    ma25 = Column(Float)
    ma75 = Column(Float)
    ma25_conf = Column(Float)
    ma75_conf = Column(Float)

    slope_atr_scaled = Column(Float)
    volume_slope = Column(Float)

    rsi = Column(Float)
    atr = Column(Float)

    final_score = Column(Float)

    max_favorable_pct = Column(Float)
    max_drawdown_pct = Column(Float)

    ai_reason = Column(String)

    created_at = Column(DateTime, default=dt.datetime.utcnow, index=True)


__all__ = [
    "Base",
    "Summary",
    "StockSummary1Min",
    "StockSummary3Min",
    "StockSummary5Min",
    "Ranking",
    "RankingRaw1Min",
    "RankingSnapshot1Min",
    "RankingMA1Min",
    "EntryLog",
    "ExitLog",
    "TradeExitStats",
    "TradeHistory",
    "Position",
    "SymbolFlags",
    "WatchlistHistory",
    "ConfidenceBias",
    "SymbolStateLog",
    "RankingEntryEvent",
    "SUMMARY_WIDE_SCHEMA_COLUMNS",
    "ensure_summary_wide_schema",
]