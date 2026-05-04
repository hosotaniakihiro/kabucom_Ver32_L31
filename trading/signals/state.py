# ============================================================
# trading/signals/state.py
# Ver24-FINAL-CLEAN-MINIMAL
# ------------------------------------------------------------
# ・prev_state は「前回バーの指標値」のみ保持
# ・フラグ（is_xxx）は一切持たない
# ・event-only scoring（False→True）用の土台
# ============================================================

from typing import Dict


# ============================================================
# 🔹 共通 state 初期化
# ============================================================

def init_state() -> Dict:
    """
    prev_state の初期値
    ※ BUY / SELL 共通
    """
    return {
        # --- moving averages ---
        "ma5": None,
        "ma25": None,
        "ma75": None,

        # --- oscillators ---
        "macd": None,
        "signal": None,
        "rsi": None,
        "rci": None,
        "rci9": None,
        "slowk": None,
        "slowd": None,

        # --- volatility ---
        "bb_upper": None,
        "bb_lower": None,
        "bb_upper3": None,
        "bb_lower3": None,

        # --- price reference ---
        "vwap": None,
        "close_price": None,
        "open_price": None,
        "high_price": None,
        "low_price": None,

        # --- volume / tick ---
        "volume": None,
        "tick_count": None,
    }


# ============================================================
# 🔹 prev_state 更新
# ============================================================

def update_state(alert_data: dict, symbol: str, curr: dict):
    """
    最新バーの値を prev_state として保存
    """
    if symbol not in alert_data:
        alert_data[symbol] = {}

    alert_data[symbol]["prev_state"] = {
        k: curr.get(k)
        for k in init_state().keys()
    }

    return alert_data


# ============================================================
# 🔹 prev_state 取得
# ============================================================

def get_prev_state(alert_data: dict, symbol: str):
    """
    prev_state を取得
    （存在しない場合は初期化）
    """
    if symbol not in alert_data:
        alert_data[symbol] = {}

    if "prev_state" not in alert_data[symbol]:
        alert_data[symbol]["prev_state"] = init_state()

    return alert_data[symbol]["prev_state"]
