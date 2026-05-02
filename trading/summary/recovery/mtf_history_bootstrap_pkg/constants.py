# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/constants.py
# Version: PRODUCTION-STABLE-REV1.1-CONSTANTS-LOCK-SAFE
# ------------------------------------------------------------
# 【概要】
#   MTF history bootstrap 用定数
#
# 【REV1.1 修正点】
#   ✔ DEFAULT_HISTORY_BARS_1M を 420 → 390 に変更
#   ✔ 5分足75MAに必要な 375分 + 余裕分のみ
#   ✔ 450分以上読み込まない方針を明示
#   ✔ 起動時1分足履歴のDB大量UPSERTを禁止する定数を追加
# ============================================================

from __future__ import annotations

DEFAULT_INTERVALS = (1, 3, 5)

# ------------------------------------------------------------
# 起動時に読み込む1分足の最大本数
# ------------------------------------------------------------
# 5分足75MA = 75 * 5 = 375分
# 余裕を見て390本。
# 450分は読まない。
DEFAULT_HISTORY_BARS_1M = 390

DEFAULT_LOOKBACK_DAYS = 3

# ------------------------------------------------------------
# 起動時1分足履歴の大量UPSERT禁止
# ------------------------------------------------------------
# interval=1 の full history は、
# 3min / 5min 再構築と global cache 表示用には使うが、
# stock_summary_1min へ大量に再UPSERTしない。
SAVE_BOOTSTRAP_1MIN_HISTORY = False

# 3min / 5min は不足分更新・再構築後の保存対象。
DEFAULT_PERSIST_INTERVALS = (3, 5)

MIN_TECH_READY_RSI = 14
MIN_TECH_READY_MACD = 26

PRICE_ALIAS_MAP = {
    "open": [
        "open",
        "open_price",
        "Open",
        "OpenPrice",
        "opening_price",
        "OpeningPrice",
    ],
    "high": [
        "high",
        "high_price",
        "High",
        "HighPrice",
    ],
    "low": [
        "low",
        "low_price",
        "Low",
        "LowPrice",
    ],
    "close": [
        "close",
        "close_price",
        "Close",
        "ClosePrice",
        "price",
        "Price",
        "current_price",
        "CurrentPrice",
        "last_price",
        "LastPrice",
    ],
}

VOLUME_ALIASES = [
    "volume",
    "Volume",
    "trading_volume",
    "TradingVolume",
    "last_cum_volume",
    "volume_total",
]