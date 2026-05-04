# ============================================================
# three_stage.py（Ver6.0）
# ------------------------------------------------------------
# BUY三段点火 / SELL逆三段点火を管理する独立モジュール
# ・state = 0 → 初期状態
# ・state = 1 → 第一波（点火）
# ・state = 2 → 押し/戻り（調整）
# ・state = 3 → 再点火成功（ENTRY）
# ============================================================

from global_state import global_data


# ------------------------------------------------------------
# BUY 三段点火用ステート
# ------------------------------------------------------------
_three_stage_buy_state = {}


def detect_three_stage_buy(symbol, price, vol_1m, ret_1m, avg3m):
    """
    BUY 三段点火ロジック：
      state 0 → 第一波点火 (vol spike + 上昇率)
      state 1 → 押し入り
      state 2 → 再上昇確認 → ENTRY
    """

    st = _three_stage_buy_state.setdefault(
        symbol,
        {"state": 0, "first_high": 0, "first_vol": 0}
    )

    # --- state 0 → 第一波 ---
    if st["state"] == 0:
        if avg3m > 0 and vol_1m >= avg3m * 3 and ret_1m >= 0.3:
            st["state"] = 1
            st["first_high"] = price
            st["first_vol"] = vol_1m
            return 1  # 第一波ヒット

    # --- state 1 → 押し（深めの下落） ---
    if st["state"] == 1:
        if price <= st["first_high"] * 0.995:
            st["state"] = 2
            return 2  # 押し入り

    # --- state 2 → 再上昇 → ENTRY条件 ---
    if st["state"] == 2:
        if price >= st["first_high"] and vol_1m >= st["first_vol"] * 0.7:
            st["state"] = 3
            return 3  # ENTRY 条件成立

    return st["state"]


# ------------------------------------------------------------
# SELL 逆三段点火用ステート
# ------------------------------------------------------------
_three_stage_sell_state = {}


def detect_three_stage_sell(symbol, price, vol_1m, ret_1m, avg3m):
    """
    SELL 逆三段点火ロジック：
      state 0 → 第一波（急落点火）
      state 1 → 戻り
      state 2 → 再急落確認 → ENTRY
    """

    st = _three_stage_sell_state.setdefault(
        symbol,
        {"state": 0, "first_low": 0, "first_vol": 0}
    )

    # --- state 0 → 第一波（急落） ---
    if st["state"] == 0:
        if avg3m > 0 and vol_1m >= avg3m * 3 and ret_1m <= -0.3:
            st["state"] = 1
            st["first_low"] = price
            st["first_vol"] = vol_1m
            return 1

    # --- state 1 → 戻り ---
    if st["state"] == 1:
        if price >= st["first_low"] * 1.005:
            st["state"] = 2
            return 2

    # --- state 2 → 再下落 → ENTRY ---
    if st["state"] == 2:
        if price <= st["first_low"]:
            st["state"] = 3
            return 3

    return st["state"]
