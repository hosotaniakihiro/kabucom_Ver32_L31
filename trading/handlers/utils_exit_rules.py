# ============================================================
# utils_exit_rules.py（EXIT判定ルール集 – Ver24-FINAL）
# ------------------------------------------------------------
# ✔ TRAIL：買い/売り 双方に対応（0.3%反転EXIT）
# ✔ VWAP：BUY のみ
# ✔ ATR：現状OFF（将来拡張可能）
# ============================================================


# ------------------------------------------------------------
# TRAILING STOP（0.3% 反転）BUY/SELL 両対応
# ------------------------------------------------------------
def judge_exit_trailing(current_price, extreme_price, side, pct=0.003):
    """
    current_price : 現在値
    extreme_price : BUY → high_since_entry / SELL → low_since_entry
    side          : "BUY_CREDIT" or "SELL_CREDIT"
    pct           : 0.003 = 0.3%

    BUY  → 最高値 × (1 - pct) を下回ったら EXIT
    SELL → 最安値 × (1 + pct) を上回ったら EXIT
    """

    if current_price is None or extreme_price is None:
        return False, None

    # BUY：最高値から pct 下落で EXIT
    if side == "BUY_CREDIT":
        trail_line = extreme_price * (1 - pct)
        if current_price <= trail_line:
            return True, f"TRAIL({pct*100:.2f}%) BUY {current_price:.2f} <= {trail_line:.2f}"

    # SELL：最安値から pct 上昇で EXIT
    else:  # SELL_CREDIT
        trail_line = extreme_price * (1 + pct)
        if current_price >= trail_line:
            return True, f"TRAIL({pct*100:.2f}%) SELL {current_price:.2f} >= {trail_line:.2f}"

    return False, None


# ------------------------------------------------------------
# VWAP 割れ EXIT（BUY のみ）
# ------------------------------------------------------------
def judge_exit_vwap(price, vwap):
    """
    BUYポジション専用
    price < vwap で EXIT
    """
    if vwap is None or vwap <= 0:
        return False, None

    if price < vwap:
        return True, "VWAP_break"

    return False, None


# ------------------------------------------------------------
# ATR ベース急落検知（現状 OFF）
# ------------------------------------------------------------
def judge_exit_atr(price, atr, mult=1.5):
    """
    ATR急落チェック（今はOFF）
    仕様追加可能：急落時保険 EXIT
    """
    return False, None
