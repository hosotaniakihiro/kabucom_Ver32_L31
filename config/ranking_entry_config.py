# ============================================================
# File: config/ranking_entry_config.py
# Ver : RANKING-ONLY-ENTRY-CONFIG-v5.0.1
# ------------------------------------------------------------
# Ranking ENTRY 専用設定ファイル
#
# ✔ ランキング由来 ENTRY の条件をすべて数値化
# ✔ ランキング表示情報と、そこから算出できる数値だけで判定
# ✔ SUMMARY / PUSH / 板 / 5秒足 / 日足MA は使わない
# ✔ BUY / SELL 両対応
# ✔ 現在値推移・順位改善・連続出現・売買代金・出来高で判定
# ✔ Ver5.0.1: 最高価格を 7,000円以下へ統一
# ============================================================

from datetime import time, datetime


# ============================================================
# メイン設定
# ============================================================

RANKING_ENTRY_CONFIG = {

    # ========================================================
    # 時間帯ガード
    # ========================================================
    "TIME_GUARD": {
        "NO_ENTRY_BEFORE": time(9, 5),
        "STRICT_UNTIL": time(9, 30),
        "NO_ENTRY_AFTER": time(14, 30),
    },

    # ========================================================
    # ランキング条件
    # ========================================================
    "RANKING": {
        # None = 全ランキング種別を許可
        "TYPE": None,

        # ENTRY を許可する最大順位
        "MAX_RANK_POSITION": 30,

        # 同一銘柄がランキングに何回以上連続出現したら許可するか
        # 1 = 初回から許可 / 2 = 2回連続出現から許可
        "MIN_CONSECUTIVE_APPEAR": 2,

        # 直近何回のランキング取得価格で高値/安値更新を見るか
        "PRICE_BREAKOUT_WINDOW": 3,

        # 前回ランキング取得データがない初回エントリーを禁止する
        "REQUIRE_PREVIOUS_SNAPSHOT": True,

        # 順位悪化を禁止する
        "REQUIRE_RANK_NOT_WORSE": True,

        # BUY は直近ランキング価格の高値更新、SELL は安値更新を要求する
        "REQUIRE_PRICE_BREAKOUT": True,
    },

    # ========================================================
    # 出来高・売買代金
    # ========================================================
    "VOLUME": {
        # ランキング由来だけで判断するため、流動性は必須
        "MIN_VOLUME": 30_000,
        "MIN_TURNOVER": 10_000_000,

        # 互換用。旧ロジックが参照しても壊れないように残す
        "MIN_SPEED_RATIO": 1.0,
    },

    # ========================================================
    # 価格帯フィルタ
    # ========================================================
    "PRICE": {
        "MIN": 300,
        "MAX": 7_000,
    },

    # ========================================================
    # 値動きフィルタ
    # ========================================================
    "PRICE_MOVE": {
        # ランキング1回分で飛びすぎた銘柄は追いかけない
        "MAX_STEP_MOVE_PCT": 1.0,

        # 前日比率が過熱しすぎた銘柄は除外
        "MAX_DAY_CHANGE_PCT": 10.0,

        # BUY / SELL の最低方向性
        "BUY_MIN_DAY_CHANGE_PCT": 0.0,
        "SELL_MAX_DAY_CHANGE_PCT": 0.0,
    },

    # ========================================================
    # ENTRY スコア
    # ========================================================
    "SCORE": {
        # ランキング専用スコアの最低ライン
        "MIN_ENTRY_SCORE": 70.0,

        # RANKING ONLY のためテクニカル評価は使わない
        "USE_TECHNICAL_SCORE": False,
        "USE_AI_GATE": False,

        # 互換用
        "MIN_DOMINANT_RATIO": 0.0,
        "ALLOW_SIDE_NONE": False,
        "SCORE_NORMALIZE": "minmax",
        "WEIGHT_SNAPSHOT": 1.0,
        "WEIGHT_TECHNICAL": 0.0,
    },
}


# ============================================================
# 内部ユーティリティ
# ============================================================

def _to_time(t) -> time:
    """
    datetime / time / None を安全に time に変換
    """
    if t is None:
        return None
    if isinstance(t, datetime):
        return t.time()
    if isinstance(t, time):
        return t
    raise TypeError(f"Unsupported time type: {type(t)}")


# ============================================================
# 補助関数（時間帯）
# ============================================================

def is_time_allowed(now) -> bool:
    """
    現在時刻が ENTRY 可能かどうか
    - datetime / time 両対応
    """
    tg = RANKING_ENTRY_CONFIG["TIME_GUARD"]
    now_t = _to_time(now)

    if now_t < tg["NO_ENTRY_BEFORE"]:
        return False
    if now_t >= tg["NO_ENTRY_AFTER"]:
        return False
    return True


def is_strict_time(now) -> bool:
    """
    条件を厳しくすべき時間帯か
    """
    tg = RANKING_ENTRY_CONFIG["TIME_GUARD"]
    now_t = _to_time(now)

    return now_t < tg["STRICT_UNTIL"]
