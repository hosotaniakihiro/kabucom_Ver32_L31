# ============================================================
# File: config/ranking_entry_config.py
# Ver : RANKING-ONLY-ENTRY-CONFIG-v5.4.0-WIDER-TOP-SCORE-UNIVERSE
# ------------------------------------------------------------
# Ranking ENTRY 専用設定ファイル
#
# Ver5.4.0:
#   - エントリー数が少ないため、スコア上位から評価する母数を拡大。
#   - ランキング上位30位まで -> 80位まで。
#   - 最終スコア70 -> 60。
#   - 空売り可否・価格・出来高・売買代金など危険ガードは維持。
# ============================================================

from datetime import time, datetime


RANKING_ENTRY_CONFIG = {
    "TIME_GUARD": {
        "NO_ENTRY_BEFORE": time(9, 5),
        "STRICT_UNTIL": time(9, 30),
        "NO_ENTRY_AFTER": time(15, 20),
    },

    "RANKING": {
        "TYPE": None,
        "MAX_RANK_POSITION": 80,
        "MIN_CONSECUTIVE_APPEAR": 1,
        "PRICE_BREAKOUT_WINDOW": 3,
        "REQUIRE_PREVIOUS_SNAPSHOT": False,
        "REQUIRE_RANK_NOT_WORSE": False,
        "REQUIRE_PRICE_BREAKOUT": False,
        "FLAT_PRICE_ALLOW_MAX_RANK": 20,
    },

    "VOLUME": {
        "MIN_VOLUME": 30_000,
        "MIN_TURNOVER": 10_000_000,
        "MIN_SPEED_RATIO": 1.0,
    },

    "PRICE": {
        "MIN": 300,
        "MAX": 7_000,
    },

    "PRICE_MOVE": {
        "MAX_STEP_MOVE_PCT": 2.5,
        "MAX_DAY_CHANGE_PCT": 12.0,
        "BUY_MIN_DAY_CHANGE_PCT": 0.0,
        "SELL_MAX_DAY_CHANGE_PCT": 0.0,
    },

    "TECHNICAL": {
        "ENABLED": True,
        "REQUIRE_READY": False,
        "REQUIRE_DIRECTION": True,
        "REQUIRE_CLOSE_VS_MA5": True,
        "REQUIRE_MA5_MA25": False,
        "REQUIRE_SLOPE": True,
        "MIN_SLOPE": 0.0,
        "REQUIRE_MACD_SIGNAL": False,
        "BUY_RSI_MAX": 86.0,
        "SELL_RSI_MIN": 14.0,
        "SCORE_WEIGHT": 2.0,
    },

    "SCORE": {
        "MIN_ENTRY_SCORE": 60.0,
        "USE_TECHNICAL_SCORE": True,
        "USE_AI_GATE": False,
        "MIN_DOMINANT_RATIO": 0.0,
        "ALLOW_SIDE_NONE": False,
        "SCORE_NORMALIZE": "minmax",
        "WEIGHT_SNAPSHOT": 1.0,
        "WEIGHT_TECHNICAL": 0.0,
    },
}


def _to_time(t) -> time:
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.time()
    if isinstance(t, time):
        return t
    raise TypeError(f"Unsupported time type: {type(t)}")


def is_time_allowed(now) -> bool:
    tg = RANKING_ENTRY_CONFIG["TIME_GUARD"]
    now_t = _to_time(now)
    if now_t < tg["NO_ENTRY_BEFORE"]:
        return False
    if now_t >= tg["NO_ENTRY_AFTER"]:
        return False
    return True


def is_strict_time(now) -> bool:
    tg = RANKING_ENTRY_CONFIG["TIME_GUARD"]
    now_t = _to_time(now)
    return now_t < tg["STRICT_UNTIL"]
