# ============================================================
# File   : trading/ranking/active_symbols/config.py
# Version: Ver1.6-ACTIVE-SYMBOLS-MIN-TURNOVER-1M
# ============================================================
from __future__ import annotations
import os
from nas_root import nas_path, nas_root


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
# 今日ランキングカテゴリ配分  (2026-09-29 設計どおりに修正)
# ------------------------------------------------------------
# ユーザー指定の設計:
#   1. 全市場・値上がり率 上位50   -> その中から 売買代金 上位35
#   2. 全市場・値下がり率 上位50   -> その中から 売買代金 上位25
#   3. 全市場・売買代金   上位50   -> その中から 上昇率 上位30 / 下落率 上位20
#   4. 全市場・TICK回数   上位50   -> その中から 上昇率 上位30
#   5. グロース市場・値上がり率 上位30
#   -> 重複排除して最大 100 銘柄
#
# 2026-09-29 まで、各ランキングを「上位50」に切る処理が無く、上位250位
# (limit_per_type=250) をまるごと母集団にしていた。実測 gainers=207 /
# losers=214 / value=342 / tick=201。
# 「値上がり率 207 銘柄の中で売買代金が最大の 30 銘柄」を選ぶと、わずかに
# 上がっただけの大型株 (7203 トヨタ / 8306 三菱UFJ / 8058 三菱商事 /
# 6758 ソニー / 6501 日立 ...) が並ぶ。値動きが小さいので ATR_1M_FILTER_NG
# で全部落ち、実際に急騰している小型株 (259A / 625A / 621A / 4980 ...) は
# 監視対象に入らない。エントリー候補と PUSH 登録銘柄の重なりは 20% だった。
ACTIVE_USE_CATEGORY_QUOTA_SELECTION = env_bool("ACTIVE_USE_CATEGORY_QUOTA_SELECTION", True)

# 各ランキングの母集団をこの順位までに切る (設計の「上位50位」)。
# 0 にすると従来どおり切らない。
ACTIVE_RANK_SLICE_N = env_int("ACTIVE_RANK_SLICE_N", 50)

ACTIVE_GAINERS_BY_VALUE_N = env_int("ACTIVE_GAINERS_BY_VALUE_N", 35)
ACTIVE_LOSERS_BY_VALUE_N = env_int("ACTIVE_LOSERS_BY_VALUE_N", 25)
ACTIVE_VALUE_BY_GAINERS_N = env_int("ACTIVE_VALUE_BY_GAINERS_N", 30)
ACTIVE_VALUE_BY_LOSERS_N = env_int("ACTIVE_VALUE_BY_LOSERS_N", 20)

# TICK 上位50 の中から上昇率上位30。
# 旧実装は ACTIVE_TICK_SUPPLEMENT_N=100 で「TICK 上位から 100 まで補充」
# (上昇率の条件が無かった)。
ACTIVE_TICK_BY_GAINERS_N = env_int("ACTIVE_TICK_BY_GAINERS_N", 30)
ACTIVE_TICK_SUPPLEMENT_N = env_int("ACTIVE_TICK_SUPPLEMENT_N", 100)

# グロース市場・値上がり率 上位30 (専用枠)。
ACTIVE_GROWTH_GAINERS_N = env_int("ACTIVE_GROWTH_GAINERS_N", 30)
GROWTH_MARKET_CODES = ("TG", "GROWTH", "グロース", "東証グロース", "G")
ALL_MARKET_CODES = ("ALL", "", "全市場")

# 順位 / 市場の列名候補。
RANK_COLUMNS = ("rank", "rank_position", "順位", "ranking", "best_rank")
MARKET_COLUMNS = ("market", "exchange", "ExchangeDivision", "市場", "exchange_division")

DEFAULT_SYMBOL_FLAGS_DB = nas_path(r"Basic\symbol_flags.db")
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
# 2026-06-29ログで 20,000,000 円では today_ranking 98銘柄が全落ちした。
# ユーザー指定の監視条件「直近5本売買代金100万円」に合わせ、監視母集団は100万円を既定にする。
MIN_TRADING_VALUE = env_float("ACTIVE_MIN_TRADING_VALUE", 1_000_000)
MIN_VOLUME = env_float("ACTIVE_MIN_VOLUME", 3_000)
MIN_TICK_COUNT = env_float("ACTIVE_MIN_TICK_COUNT", 10)
# 監視銘柄は200円以上に限定。
MIN_PRICE = env_float("ACTIVE_MIN_PRICE", 200)
# 監視銘柄は7000円以下に限定。0以下にすると上限なし。
MAX_PRICE = env_float("ACTIVE_MAX_PRICE", 7_000)
KEEP_PROTECTED_EVEN_IF_ILLIQUID = env_bool("ACTIVE_KEEP_PROTECTED_EVEN_IF_ILLIQUID", True)

PRICE_COLUMNS = ("current_price", "price", "close", "close_price", "現在値")
VOLUME_COLUMNS = ("trading_volume", "volume", "出来高")
VALUE_COLUMNS = ("trading_value", "turnover", "売買代金")
TICK_COLUMNS = ("tick_count", "tick", "ticks", "TICK回数")
CHANGE_COLUMNS = ("change_percentage", "change_ratio", "change_rate", "騰落率", "前日比率", "前日比%", "change_pct")
RANK_TYPE_COLUMNS = ("ranking_type", "rank_type", "type", "category", "ランキング種別", "ranking_name")
