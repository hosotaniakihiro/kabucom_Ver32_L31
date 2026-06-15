# ============================================================
# File   : trading/ranking/active_symbols/config.py
# Version: Ver1.4-ACTIVE-SYMBOLS-PREMARKET-ONLY-BEFORE-OPEN
# ============================================================
from __future__ import annotations
import os


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
        return True
    if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}:
        return False
    return bool(default)


def env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(str(v).strip()))
    except Exception:
        return int(default)


def env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(default) if v is None or str(v).strip() == "" else float(str(v).strip())
    except Exception:
        return float(default)


TARGET_ACTIVE_SYMBOLS = env_int("ACTIVE_TARGET_SYMBOLS", 100)
MAX_ACTIVE_SYMBOLS = env_int("ACTIVE_MAX_SYMBOLS", 100)
RANKING_EXPIRE_MINUTES = env_int("ACTIVE_RANKING_EXPIRE_MINUTES", 20)
VOLUME_SPEED_TOP_N = env_int("ACTIVE_VOLUME_SPEED_TOP_N", 10)

# ------------------------------------------------------------
# 今日ランキングカテゴリ配分
# ------------------------------------------------------------
# 1. 値上がり率ランキング内で売買代金上位30
# 2. 値下がり率ランキング内で売買代金上位10
# 3. 売買代金ランキング内で上昇率上位30
# 4. 売買代金ランキング内で下落率上位10
# 5. 残りはTICK上位で100まで補充
ACTIVE_USE_CATEGORY_QUOTA_SELECTION = env_bool("ACTIVE_USE_CATEGORY_QUOTA_SELECTION", True)
ACTIVE_GAINERS_BY_VALUE_N = env_int("ACTIVE_GAINERS_BY_VALUE_N", 30)
ACTIVE_LOSERS_BY_VALUE_N = env_int("ACTIVE_LOSERS_BY_VALUE_N", 10)
ACTIVE_VALUE_BY_GAINERS_N = env_int("ACTIVE_VALUE_BY_GAINERS_N", 30)
ACTIVE_VALUE_BY_LOSERS_N = env_int("ACTIVE_VALUE_BY_LOSERS_N", 10)
ACTIVE_TICK_SUPPLEMENT_N = env_int("ACTIVE_TICK_SUPPLEMENT_N", 100)

DEFAULT_SYMBOL_FLAGS_DB = r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db"
SYMBOL_FLAGS_DB = os.environ.get("SYMBOL_FLAGS_DB_PATH", DEFAULT_SYMBOL_FLAGS_DB)
ACTIVE_REQUIRE_SYMBOL_FLAGS = env_bool("ACTIVE_REQUIRE_SYMBOL_FLAGS", True)
ACTIVE_ALLOW_BUY_TARGET = env_bool("ACTIVE_ALLOW_BUY_TARGET", True)
ACTIVE_ALLOW_SELL_TARGET = env_bool("ACTIVE_ALLOW_SELL_TARGET", True)
ACTIVE_EXCLUDE_ETF = env_bool("ACTIVE_EXCLUDE_ETF", True)

ENABLE_PREMARKET_SBI = env_bool("ACTIVE_ENABLE_PREMARKET_SBI", True)
PREMARKET_START_HOUR = env_int("ACTIVE_PREMARKET_START_HOUR", 7)
PREMARKET_START_MINUTE = env_int("ACTIVE_PREMARKET_START_MINUTE", 0)
PREMARKET_END_HOUR = env_int("ACTIVE_PREMARKET_END_HOUR", 9)
PREMARKET_END_MINUTE = env_int("ACTIVE_PREMARKET_END_MINUTE", 0)
# 寄前SBI 100銘柄は寄り前だけ使用する。ザラ場中はランキングを参照する。
# どうしてもランキング空時にSBIへ戻したい場合のみ 1 にする。
USE_PREMARKET_WHEN_TODAY_RANKING_EMPTY = env_bool("ACTIVE_USE_PREMARKET_WHEN_TODAY_RANKING_EMPTY", False)
PREMARKET_ALLOW_NO_PRICE = env_bool("ACTIVE_PREMARKET_ALLOW_NO_PRICE", True)

ENABLE_LIQUIDITY_FILTER = env_bool("ACTIVE_ENABLE_LIQUIDITY_FILTER", True)
MIN_TRADING_VALUE = env_float("ACTIVE_MIN_TRADING_VALUE", 20_000_000)
MIN_VOLUME = env_float("ACTIVE_MIN_VOLUME", 3_000)
MIN_TICK_COUNT = env_float("ACTIVE_MIN_TICK_COUNT", 10)
# 監視銘柄は2500円以上に限定。
MIN_PRICE = env_float("ACTIVE_MIN_PRICE", 2_500)
# 監視銘柄は7000円以下に限定。0以下にすると上限なし。
MAX_PRICE = env_float("ACTIVE_MAX_PRICE", 7_000)
KEEP_PROTECTED_EVEN_IF_ILLIQUID = env_bool("ACTIVE_KEEP_PROTECTED_EVEN_IF_ILLIQUID", True)

PRICE_COLUMNS = ("current_price", "price", "close", "close_price", "現在値")
VOLUME_COLUMNS = ("trading_volume", "volume", "出来高")
VALUE_COLUMNS = ("trading_value", "turnover", "売買代金")
TICK_COLUMNS = ("tick_count", "tick", "ticks", "TICK回数")
CHANGE_COLUMNS = ("change_percentage", "change_ratio", "change_rate", "騰落率", "前日比率", "前日比%", "change_pct")
RANK_TYPE_COLUMNS = ("ranking_type", "rank_type", "type", "category", "ランキング種別", "ranking_name")