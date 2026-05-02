# ============================================================
# File   : trading/yahoo/pipeline/complement/constants.py
# Version: PRODUCTION-STABLE-REV4.1-YAHOO-COMPLEMENT-CONSTANTS
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完パイプライン共通定数
#
# 【主な機能】
#   - summary DB保存先テーブル名
#   - PUSH由来 / Yahoo由来 source 名
#   - 保存対象interval
#   - warmup / overlap / recent除外設定
#   - Yahoo補完保存時の低優先UPSERT設定
#
# 【設計方針】
#   - runner / db / diff / save / compute から共通参照する
#   - source名をここで一元管理する
#   - 既存の source 命名規則:
#       summary_recovery_push_1m
#       summary_recovery_resample_3m
#       summary_recovery_resample_5m
#     に合わせて Yahoo補完 source を定義する
#
# 【Yahoo補完 source】
#   - summary_recovery_yahoo_1m
#   - summary_recovery_yahoo_resample_3m
#   - summary_recovery_yahoo_resample_5m
# ============================================================

from __future__ import annotations

import datetime as dt


# ============================================================
# base paths
# ============================================================

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"


# ============================================================
# intervals
# ============================================================

DEFAULT_INTERVALS: tuple[int, ...] = (1, 3, 5)

SUPPORTED_INTERVALS: tuple[int, ...] = (1, 3, 5)


# ============================================================
# summary DB tables
# ============================================================

SUMMARY_TABLE_BY_INTERVAL: dict[int, str] = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}


# ============================================================
# source names
# ============================================================

PUSH_SUMMARY_SOURCE_BY_INTERVAL: dict[int, str] = {
    1: "summary_recovery_push_1m",
    3: "summary_recovery_resample_3m",
    5: "summary_recovery_resample_5m",
}

YAHOO_SUMMARY_SOURCE_BY_INTERVAL: dict[int, str] = {
    1: "summary_recovery_yahoo_1m",
    3: "summary_recovery_yahoo_resample_3m",
    5: "summary_recovery_yahoo_resample_5m",
}

# 後方互換用 alias
SUMMARY_SOURCE_BY_INTERVAL = YAHOO_SUMMARY_SOURCE_BY_INTERVAL
PUSH_SOURCE_BY_INTERVAL = PUSH_SUMMARY_SOURCE_BY_INTERVAL


# ============================================================
# diff / warmup settings
# ============================================================

# テクニカル指標計算用の過去読み込み。
# MA75 / MACD / RSI / slope などの安定化を考慮して多めに読む。
DEFAULT_WARMUP_MINUTES = 140

# Yahooの後追い修正を拾うため、保存済みYahoo最新時刻から少し戻して再保存する。
DEFAULT_OVERLAP_MINUTES_BY_INTERVAL: dict[int, int] = {
    1: 5,
    3: 15,
    5: 25,
}

# 直近未確定帯は触らない。
# 例: 現在 10:00 の場合、20分設定なら 09:40 以前のみ保存対象。
DEFAULT_TOUCH_RECENT_MINUTES = 20


# ============================================================
# save / upsert settings
# ============================================================

# Yahoo補完は低優先。
# PUSH保存や定時summary保存を詰まらせないため short-timeout + busy skip。
YAHOO_SUMMARY_LOCK_TIMEOUT_SEC = 3.0
YAHOO_SUMMARY_SKIP_IF_BUSY = True


# ============================================================
# market session
# ============================================================

MARKET_AM_START = dt.time(9, 0)
MARKET_AM_END = dt.time(11, 30)
MARKET_PM_START = dt.time(12, 30)
MARKET_PM_END = dt.time(15, 30)


# ============================================================
# schema preferred columns
# ============================================================

PREFERRED_SUMMARY_COLUMNS: list[str] = [
    "symbol",
    "symbolname",
    "datetime",
    "date",
    "time_range",
    "time",
    "start_time",
    "end_time",
    "interval",
    "source",
    "signal",

    # OHLCV
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_price",
    "high_price",
    "low_price",
    "close_price",

    # indicators
    "vwap",
    "ma5",
    "ma25",
    "ma75",
    "ma5_conf",
    "ma25_conf",
    "ma75_conf",
    "ma75_slope",
    "volume_slope",
    "vwap_slope",
    "ema12",
    "ema26",
    "macd",
    "hist",
    "rsi",
    "rci",
    "atr",
    "atr_1m",
    "atr_3m",
    "atr_5m",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "bb_width",

    # scores
    "price_diff",
    "score",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "score_total",
    "score_slope",
    "score_mtf",
    "slope",
    "slope_atr_scaled",
    "slope_atr_scaled_1m",
    "slope_atr_scaled_3m",
    "slope_atr_scaled_5m",
    "mtf",
    "base",
    "trend",
    "mom",
    "vel",
    "pen",
    "combined_score",
    "buy_score",
    "sell_score",

    # flags / metadata
    "symbol_hist_len",
    "technical_ready",
    "last_update",
]


# ============================================================
# numeric columns
# ============================================================

NUMERIC_COLUMNS: list[str] = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "price",
    "current_price",
    "CurrentPrice",
    "last_price",
    "LastPrice",
    "trading_volume",
    "TradingVolume",
    "tradingvolume",

    "vwap",
    "ma5",
    "ma25",
    "ma75",
    "ma5_conf",
    "ma25_conf",
    "ma75_conf",
    "ma75_slope",
    "volume_slope",
    "vwap_slope",
    "ema12",
    "ema26",
    "macd",
    "hist",
    "rsi",
    "rci",
    "atr",
    "atr_1m",
    "atr_3m",
    "atr_5m",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "bb_width",

    "price_diff",
    "score",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "score_total",
    "score_slope",
    "score_mtf",
    "slope",
    "slope_atr_scaled",
    "slope_atr_scaled_1m",
    "slope_atr_scaled_3m",
    "slope_atr_scaled_5m",
    "mtf",
    "base",
    "trend",
    "mom",
    "vel",
    "pen",
    "combined_score",
    "buy_score",
    "sell_score",
    "symbol_hist_len",
    "technical_ready",
]


# ============================================================
# aliases
# ============================================================

OHLCV_ALIAS_MAP: dict[str, str] = {
    "time": "datetime",
    "Datetime": "datetime",
    "timestamp": "datetime",
    "date_time": "datetime",
    "日時": "datetime",
    "日付": "datetime",

    "code": "symbol",
    "ticker": "symbol",
    "銘柄コード": "symbol",

    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",

    "始値": "open",
    "高値": "high",
    "安値": "low",
    "終値": "close",
    "現在値": "close",
    "出来高": "volume",

    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "close_price": "close",

    "price": "close",
    "current_price": "close",
    "currentprice": "close",
    "CurrentPrice": "close",
    "last_price": "close",
    "lastprice": "close",
    "LastPrice": "close",

    "trading_volume": "volume",
    "tradingvolume": "volume",
    "TradingVolume": "volume",
}


# ============================================================
# public helpers
# ============================================================

def normalize_interval(interval: int | str) -> int:
    """
    intervalをint化する。
    '1min' / '3min' / '5min' のような文字列も許容する。
    """
    try:
        if isinstance(interval, str):
            s = interval.strip().lower().replace("min", "").replace("m", "")
            return int(s)
        return int(interval)
    except Exception:
        return 1


def yahoo_source_for_interval(interval: int | str) -> str:
    iv = normalize_interval(interval)
    return YAHOO_SUMMARY_SOURCE_BY_INTERVAL.get(
        iv,
        f"summary_recovery_yahoo_{iv}m",
    )


def push_source_for_interval(interval: int | str) -> str:
    iv = normalize_interval(interval)
    return PUSH_SUMMARY_SOURCE_BY_INTERVAL.get(
        iv,
        f"summary_recovery_push_{iv}m",
    )


def summary_table_for_interval(interval: int | str) -> str:
    iv = normalize_interval(interval)
    return SUMMARY_TABLE_BY_INTERVAL.get(
        iv,
        f"stock_summary_{iv}min",
    )


__all__ = [
    "DEFAULT_BASE_DIR",
    "DEFAULT_INTERVALS",
    "SUPPORTED_INTERVALS",
    "SUMMARY_TABLE_BY_INTERVAL",
    "PUSH_SUMMARY_SOURCE_BY_INTERVAL",
    "YAHOO_SUMMARY_SOURCE_BY_INTERVAL",
    "SUMMARY_SOURCE_BY_INTERVAL",
    "PUSH_SOURCE_BY_INTERVAL",
    "DEFAULT_WARMUP_MINUTES",
    "DEFAULT_OVERLAP_MINUTES_BY_INTERVAL",
    "DEFAULT_TOUCH_RECENT_MINUTES",
    "YAHOO_SUMMARY_LOCK_TIMEOUT_SEC",
    "YAHOO_SUMMARY_SKIP_IF_BUSY",
    "MARKET_AM_START",
    "MARKET_AM_END",
    "MARKET_PM_START",
    "MARKET_PM_END",
    "PREFERRED_SUMMARY_COLUMNS",
    "NUMERIC_COLUMNS",
    "OHLCV_ALIAS_MAP",
    "normalize_interval",
    "yahoo_source_for_interval",
    "push_source_for_interval",
    "summary_table_for_interval",
]