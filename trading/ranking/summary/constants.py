# ============================================================
# File   : trading/ranking/summary/constants.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-CONSTANTS
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリー用の定数定義
# ============================================================

from __future__ import annotations

SUPPORTED_INTERVALS = (1, 3, 5)

RANKING_SNAPSHOT_TABLE = "ranking_snapshot_1min"

DEFAULT_LOOKBACK_MINUTES = 240
DEFAULT_TOP_N = 10

DATETIME_CANDIDATES = (
    "datetime",
    "snapshot_time",
    "inserted_at",
    "created_at",
    "time",
    "timestamp",
)

PRICE_CANDIDATES = (
    "current_price",
    "price",
    "close",
    "last_price",
)

VOLUME_CANDIDATES = (
    "trading_volume",
    "volume",
    "volume_1m",
)

TURNOVER_CANDIDATES = (
    "trading_value",
    "turnover",
    "turnover_value",
)

RANK_CANDIDATES = (
    "rank_position",
    "rank",
    "best_rank_position",
)

TYPE_CANDIDATES = (
    "ranking_type",
    "rank_type",
    "category",
    "source",
    "type",
)

CHANGE_CANDIDATES = (
    "change_percentage",
    "change_rate",
    "price_delta_1m",
)

OPTIONAL_NUMERIC_COLS = [
    "volume_speed",
    "rank_strength",
    "rank_persistence",
    "rank_delta",
    "price_delta_1m",
    "volume_delta_1m",
    "volume_spike",
    "minute_of_day",
    "trading_value",
    "turnover",
]