# ============================================================
# File   : trading/ranking/summary/bootstrap_config.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-CONFIG
# ------------------------------------------------------------
# 【概要】
#   ranking summary bootstrap 用の定数・パス・列定義
#
# 【方針】
#   - PUSH由来 summary DB は読むだけ
#   - ranking snapshot DB は読むだけ
#   - ranking summary DB に保存する
#   - ranking snapshot 由来の OHLC はすべて同値
#     open = high = low = close = snapshot price
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"

DEFAULT_INTERVALS = (1, 3, 5)

DEFAULT_LOOKBACK_MINUTES_1M = 240
DEFAULT_LOOKBACK_MINUTES_3M = 480
DEFAULT_LOOKBACK_MINUTES_5M = 600
DEFAULT_LOOKBACK_MINUTES = 600

RANKING_SNAPSHOT_TABLE = "ranking_snapshot_1min"

RANKING_SUMMARY_TABLES = {
    1: "ranking_summary_1min",
    3: "ranking_summary_3min",
    5: "ranking_summary_5min",
}

PUSH_SUMMARY_TABLES = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

PRICE_CANDIDATES = [
    "current_price",
    "last_price",
    "price",
    "close",
    "現在値",
    "株価",
    "first_price",
    "max_price",
    "min_price",
]

VOLUME_CANDIDATES = [
    "trading_volume",
    "volume",
    "sum_volume",
    "売買高",
]

TRADING_VALUE_CANDIDATES = [
    "trading_value",
    "売買代金",
    "turnover_value",
]

TICK_COUNT_CANDIDATES = [
    "tick_count",
    "ticks",
    "TICK回数",
]

RANK_POSITION_CANDIDATES = [
    "rank_position",
    "rank",
    "順位",
    "position",
]

RANK_TYPE_CANDIDATES = [
    "rank_type",
    "ranking_type",
    "type",
    "ランキング種別",
]

SYMBOLNAME_CANDIDATES = [
    "symbolname",
    "symbol_name",
    "name",
    "銘柄名",
    "SymbolName",
]

SUMMARY_COLUMNS = [
    "symbol",
    "symbolname",
    "datetime",
    "interval",

    "open",
    "high",
    "low",
    "close",
    "volume",
    "trading_value",
    "turnover",
    "tick_count",

    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "signal",
    "hist",
    "atr",
    "vwap",
    "slope",
    "slope_atr_scaled",

    "score",
    "score_total",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "score_slope",
    "score_mtf",

    "best_rank_position",
    "last_rank_position",
    "avg_rank_position",
    "rank_count",
    "rank_types",

    "technical_ready",
    "hist_len",
    "source",
    "updated_at",
]


NUMERIC_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trading_value",
    "turnover",
    "tick_count",

    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "signal",
    "hist",
    "atr",
    "vwap",
    "slope",
    "slope_atr_scaled",

    "score",
    "score_total",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "score_slope",
    "score_mtf",

    "best_rank_position",
    "last_rank_position",
    "avg_rank_position",
]

INTEGER_COLUMNS = [
    "interval",
    "rank_count",
    "technical_ready",
    "hist_len",
]

TEXT_COLUMNS = [
    "symbol",
    "symbolname",
    "datetime",
    "rank_types",
    "source",
    "updated_at",
]


@dataclass
class RankingSummaryBootstrapPaths:
    base_dir: str
    yyyymmdd: str
    ranking_db_path: str
    summary_db_path: str
    ranking_summary_db_path: str


@dataclass
class RankingSummaryBootstrapResult:
    ok: bool
    intervals: dict[int, int]
    db_path: str | None = None
    snapshot_rows: int = 0
    message: str = ""


def today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def to_yyyymmdd(date_like: Any | None = None) -> str:
    if date_like is None:
        return today_yyyymmdd()

    if isinstance(date_like, str):
        s = date_like.strip()
        if len(s) == 8 and s.isdigit():
            return s
        try:
            return pd.to_datetime(s).strftime("%Y%m%d")
        except Exception:
            return today_yyyymmdd()

    try:
        return pd.to_datetime(date_like).strftime("%Y%m%d")
    except Exception:
        return today_yyyymmdd()


def ranking_db_path(base_dir: str, yyyymmdd: str) -> str:
    return str(Path(base_dir) / "raw_data" / "kabu_station" / "ranking" / f"ranking{yyyymmdd}.db")


def summary_db_path(base_dir: str, yyyymmdd: str) -> str:
    return str(Path(base_dir) / "raw_data" / "kabu_station" / "summary" / f"summary{yyyymmdd}.db")


def ranking_summary_db_path(base_dir: str, yyyymmdd: str) -> str:
    return str(
        Path(base_dir)
        / "raw_data"
        / "kabu_station"
        / "ranking_summary"
        / f"ranking_summary{yyyymmdd}.db"
    )


def build_bootstrap_paths(
    *,
    base_dir: str = DEFAULT_BASE_DIR,
    yyyymmdd: str | None = None,
    ranking_db_path_override: str | None = None,
    summary_db_path_override: str | None = None,
    ranking_summary_db_path_override: str | None = None,
) -> RankingSummaryBootstrapPaths:
    ymd = to_yyyymmdd(yyyymmdd)

    return RankingSummaryBootstrapPaths(
        base_dir=base_dir,
        yyyymmdd=ymd,
        ranking_db_path=ranking_db_path_override or ranking_db_path(base_dir, ymd),
        summary_db_path=summary_db_path_override or summary_db_path(base_dir, ymd),
        ranking_summary_db_path=ranking_summary_db_path_override or ranking_summary_db_path(base_dir, ymd),
    )


__all__ = [
    "DEFAULT_BASE_DIR",
    "DEFAULT_INTERVALS",
    "DEFAULT_LOOKBACK_MINUTES_1M",
    "DEFAULT_LOOKBACK_MINUTES_3M",
    "DEFAULT_LOOKBACK_MINUTES_5M",
    "DEFAULT_LOOKBACK_MINUTES",
    "RANKING_SNAPSHOT_TABLE",
    "RANKING_SUMMARY_TABLES",
    "PUSH_SUMMARY_TABLES",
    "PRICE_CANDIDATES",
    "VOLUME_CANDIDATES",
    "TRADING_VALUE_CANDIDATES",
    "TICK_COUNT_CANDIDATES",
    "RANK_POSITION_CANDIDATES",
    "RANK_TYPE_CANDIDATES",
    "SYMBOLNAME_CANDIDATES",
    "SUMMARY_COLUMNS",
    "NUMERIC_COLUMNS",
    "INTEGER_COLUMNS",
    "TEXT_COLUMNS",
    "RankingSummaryBootstrapPaths",
    "RankingSummaryBootstrapResult",
    "today_yyyymmdd",
    "to_yyyymmdd",
    "ranking_db_path",
    "summary_db_path",
    "ranking_summary_db_path",
    "build_bootstrap_paths",
]