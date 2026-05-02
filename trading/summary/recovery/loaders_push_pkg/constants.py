# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/constants.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-CONSTANTS
# ------------------------------------------------------------
# 【概要】
#   PUSH loader 用定数
#
# 【主な機能】
#   ✔ PUSH DB デフォルトパス
#   ✔ PUSH table 候補
#   ✔ 市場時間定義
#   ✔ tick_time 候補列
#   ✔ symbol 候補列
# ============================================================

from __future__ import annotations


DEFAULT_PUSH_DB_DIR = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\push"

DEFAULT_PUSH_TABLE_CANDIDATES = (
    "stream_data",
    "push_data",
    "ticks",
    "push",
)

MARKET_AM_START = 900
MARKET_AM_END = 1130
MARKET_PM_START = 1230
MARKET_PM_END = 1530

DEFAULT_SYMBOL_CHUNK_SIZE = 300

PUSH_TIME_COLUMN_CANDIDATES = (
    "tick_time",
    "datetime",
    "time",
    "CurrentPriceTime",
    "current_price_time",
    "current_time",
    "received_at",
    "inserted_at",
    "created_at",
    "timestamp",
)

PUSH_SYMBOL_COLUMN_CANDIDATES = (
    "symbol",
    "Symbol",
    "code",
    "symbol_code",
)


__all__ = [
    "DEFAULT_PUSH_DB_DIR",
    "DEFAULT_PUSH_TABLE_CANDIDATES",
    "MARKET_AM_START",
    "MARKET_AM_END",
    "MARKET_PM_START",
    "MARKET_PM_END",
    "DEFAULT_SYMBOL_CHUNK_SIZE",
    "PUSH_TIME_COLUMN_CANDIDATES",
    "PUSH_SYMBOL_COLUMN_CANDIDATES",
]