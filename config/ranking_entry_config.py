# ============================================================
# File: config/ranking_entry_config.py
# ------------------------------------------------------------
# Ranking ENTRY 専用設定ファイル
#
# ✔ ランキング由来 ENTRY の条件をすべて数値化
# ✔ ロジック側に魔法の数値を残さない
# ✔ 検証・ABテスト・AI学習にそのまま利用可能
# ✔ RANKING 専用で SUMMARY より条件を緩める（Option A）
# ✔ BUY / SELL 両対応（SIDE_NONE は許可しない）
# ✔ datetime / time 両対応（安全）
# ✔ ★ snapshot × technical の重み付きスコア統合に対応（NEW）
# ============================================================

from datetime import time, datetime


# ============================================================
# メイン設定
# ============================================================

RANKING_ENTRY_CONFIG = {

    # ========================================================
    # 時間帯ガード（超重要）
    # ========================================================
    "TIME_GUARD": {
        # この時刻までは一切 ENTRY しない
        "NO_ENTRY_BEFORE": time(9, 5),

        # この時間帯は条件を厳しくする
        "STRICT_UNTIL": time(9, 30),

        # この時刻以降は新規 ENTRY 禁止
        "NO_ENTRY_AFTER": time(14, 30),
    },

    # ========================================================
    # ランキング条件（コア）
    # ========================================================
    "RANKING": {
        # 対象ランキング種別
        # None = 全ランキング種別を許可
        # 例: "値上がり率", "値下がり率"
        "TYPE": None,

        # ENTRY を許可する最大順位
        # ranking_snapshot / ranking_raw に rank_position が
        # 含まれる場合のみ使用される（未使用でも安全）
        "MAX_RANK_POSITION": 30,
    },

    # ========================================================
    # 出来高・板の勢い（RANKING 専用で緩和）
    # ========================================================
    "VOLUME": {
        # volume_speed_ratio の下限
        # SUMMARY より緩める
        "MIN_SPEED_RATIO": 1.0,

        # 売買代金（turnover）の最低ライン
        # RANKING 主導のため SUMMARY より大幅に緩和
        "MIN_TURNOVER": 1_000_000,
    },

    # ========================================================
    # MA75 フィルタ（SUMMARY と同一思想）
    # ========================================================
    "MA75": {
        # MA75_conf の最低値
        "MIN_CONF": 0.60,

        # ma75_hard_ng が True の場合は即 NG
        "USE_HARD_NG": True,
    },

    # ========================================================
    # 価格帯フィルタ
    # ========================================================
    "PRICE": {
        "MIN": 150,
        "MAX": 10_000,
    },

    # ========================================================
    # ENTRY スコア（RANKING 専用・拡張版）
    # ========================================================
    "SCORE": {
        # ----------------------------------------------------
        # 最低スコア（最終 score_total に対して適用）
        # ----------------------------------------------------
        "MIN_ENTRY_SCORE": 0.0,

        # dominant_ratio は RANKING では拘束しない
        "MIN_DOMINANT_RATIO": 0.0,

        # SIDE が確定しないものは DROP（安全側）
        # ※ entry_from_ranking 側で DEFAULT BUY を入れる設計
        "ALLOW_SIDE_NONE": False,

        # ----------------------------------------------------
        # ★ NEW：テクニカルスコア統合
        # ----------------------------------------------------

        # テクニカル評価を使うか
        "USE_TECHNICAL_SCORE": True,

        # 正規化方式
        # "minmax" or "zscore"
        "SCORE_NORMALIZE": "minmax",

        # 重み（snapshot × technical）
        # ※ 合計が 1.0 でなくても可（内部でそのまま使用）
        "WEIGHT_SNAPSHOT": 0.4,
        "WEIGHT_TECHNICAL": 0.6,
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