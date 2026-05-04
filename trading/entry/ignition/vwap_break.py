# ============================================================
# vwap_break.py（Ver6.0）
# ------------------------------------------------------------
# VWAP ブレイク判定
# ・BUY  : close_price > vwap
# ・SELL : close_price < vwap
# ============================================================

from global_state import global_data


def _get_latest_row(symbol):
    """
    最新サマリーから該当銘柄の行を取り出す安全関数。
    """
    df = global_data.get_latest_summary()
    if df is None or df.empty:
        return None

    r = df[df["symbol"] == symbol]
    if r.empty:
        return None

    return r.iloc[-1]


# ------------------------------------------------------------
# BUY VWAPブレイク
# ------------------------------------------------------------
def is_vwap_break_buy(symbol):
    """
    BUY点火条件：
        close_price > VWAP
    """
    row = _get_latest_row(symbol)
    if row is None:
        return False

    try:
        return row["close_price"] > row["vwap"]
    except:
        return False


# ------------------------------------------------------------
# SELL VWAPブレイク
# ------------------------------------------------------------
def is_vwap_break_sell(symbol):
    """
    SELL点火条件：
        close_price < VWAP
    """
    row = _get_latest_row(symbol)
    if row is None:
        return False

    try:
        return row["close_price"] < row["vwap"]
    except:
        return False
