# ============================================================
# AI/risk/lot_sizer.py
# ------------------------------------------------------------
# ✔ pred と expected_value からロット倍率算出
# ✔ 下限/上限ガード付き
# ============================================================

def calc_lot_multiplier(
    pred: float,
    expected_value: float,
    base: float = 1.0,
    min_mul: float = 0.5,
    max_mul: float = 2.0,
):
    if pred is None or expected_value is None:
        return base

    score = max(0.0, pred) * max(0.0, expected_value) * 100
    mul = base + score
    return max(min_mul, min(max_mul, mul))
