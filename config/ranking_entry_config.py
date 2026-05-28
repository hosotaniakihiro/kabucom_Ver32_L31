# ============================================================
# File: config/ranking_entry_config.py
# Ver : RANKING-ONLY-ENTRY-CONFIG-v5.3.0-EARLY-SESSION-RELAX
# ------------------------------------------------------------
# Ranking ENTRY 専用設定ファイル
#
# Ver5.3.0:
#   - 2026-05-28 09:07〜09:10ログで ranking prefilter 後101件が全落ち。
#   - 主因は寄り付き直後の履歴不足/短期ノイズに対して、
#       RANK_WORSE / RECENT_HIGH_LOW / slope=0 NG / MA5-MA25 厳格判定
#     が強すぎること。
#   - 価格/出来高/売買代金/ランキング上位/日中方向は維持しつつ、
#     まず候補を entry_controller へ流せるようランキング専用条件を緩和する。
#
# Ver5.2.0:
#   - 再起動直後に in-memory ranking_entry_history が空だと、
#     NO_PREVIOUS_RANKING_SNAPSHOT で134件以上が一括DROPされる問題を修正。
#   - ランキングDB fallback/prefilter が動いているため、前回メモリ履歴を必須にしない。
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
        "MIN_CONSECUTIVE_APPEAR": 1,
        "PRICE_BREAKOUT_WINDOW": 3,
        "REQUIRE_PREVIOUS_SNAPSHOT": False,
        # 09:05〜09:30は履歴が短く RANK_WORSE が過剰に効くため、
        # entry_from_ranking 側で履歴が十分な場合のみ参考にする。
        "REQUIRE_RANK_NOT_WORSE": False,
        # ランキング由来はランキング情報と現在値が主情報。直近高値/安値突破を必須にすると
        # 寄り付き直後に BUY_NOT_RECENT_HIGH / SELL_NOT_RECENT_LOW で全落ちするため緩和。
        "REQUIRE_PRICE_BREAKOUT": False,
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
        # 09:07〜09:10では step_pct が1%を少し超えるだけで上位候補が落ちていたため緩和。
        "MAX_STEP_MOVE_PCT": 2.0,
        "MAX_DAY_CHANGE_PCT": 10.0,
        "BUY_MIN_DAY_CHANGE_PCT": 0.0,
        "SELL_MAX_DAY_CHANGE_PCT": 0.0,
    },

    "TECHNICAL": {
        "ENABLED": True,
        "REQUIRE_READY": False,
        "REQUIRE_DIRECTION": True,
        "REQUIRE_CLOSE_VS_MA5": True,
        # 09:05〜09:30は ma5/ma25 が同値または本数不足になりやすいので必須を外す。
        "REQUIRE_MA5_MA25": False,
        "REQUIRE_SLOPE": True,
        # slope=0.000000 で SELL/BUY が一括NGになっていたため、0を中立として許容。
        "MIN_SLOPE": 0.0,
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
