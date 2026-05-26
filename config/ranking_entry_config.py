# ============================================================
# File: config/ranking_entry_config.py
# Ver : RANKING-ONLY-ENTRY-CONFIG-v5.1.1-LATE-SESSION
# ------------------------------------------------------------
# Ranking ENTRY 専用設定ファイル
#
# Ver5.1.1:
#   - NO_ENTRY_AFTER を 14:30 → 15:20 に変更。
#   - 14:47ログで RANKING ENTRY が TIME_GUARD により即skipされ、
#     ranking flat price patch の効果確認もできなかったため。
#   - 大引け直前の新規発注は避けるため 15:20 で停止する。
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
        "MAX_RANK_POSITION": 30,
        "MIN_CONSECUTIVE_APPEAR": 2,
        "PRICE_BREAKOUT_WINDOW": 3,
        "REQUIRE_PREVIOUS_SNAPSHOT": True,
        "REQUIRE_RANK_NOT_WORSE": True,
        "REQUIRE_PRICE_BREAKOUT": True,
        # 価格横ばい時の救済: ranking_entry_flat_price_guard_patch が参照。
        "FLAT_PRICE_ALLOW_MAX_RANK": 12,
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
        "MAX_STEP_MOVE_PCT": 1.0,
        "MAX_DAY_CHANGE_PCT": 10.0,
        "BUY_MIN_DAY_CHANGE_PCT": 0.0,
        "SELL_MAX_DAY_CHANGE_PCT": 0.0,
    },

    "TECHNICAL": {
        "ENABLED": True,
        "REQUIRE_READY": False,
        "REQUIRE_DIRECTION": True,
        "REQUIRE_CLOSE_VS_MA5": True,
        "REQUIRE_MA5_MA25": True,
        "REQUIRE_SLOPE": True,
        "MIN_SLOPE": 0.0001,
        "REQUIRE_MACD_SIGNAL": False,
        "BUY_RSI_MAX": 82.0,
        "SELL_RSI_MIN": 18.0,
        "SCORE_WEIGHT": 2.0,
    },

    "SCORE": {
        "MIN_ENTRY_SCORE": 70.0,
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
    """datetime / time / None を安全に time に変換"""
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.time()
    if isinstance(t, time):
        return t
    raise TypeError(f"Unsupported time type: {type(t)}")


def is_time_allowed(now) -> bool:
    """現在時刻が ENTRY 可能かどうか。datetime / time 両対応。"""
    tg = RANKING_ENTRY_CONFIG["TIME_GUARD"]
    now_t = _to_time(now)
    if now_t < tg["NO_ENTRY_BEFORE"]:
        return False
    if now_t >= tg["NO_ENTRY_AFTER"]:
        return False
    return True


def is_strict_time(now) -> bool:
    """条件を厳しくすべき時間帯か。"""
    tg = RANKING_ENTRY_CONFIG["TIME_GUARD"]
    now_t = _to_time(now)
    return now_t < tg["STRICT_UNTIL"]
