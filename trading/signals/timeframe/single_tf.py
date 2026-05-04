# ============================================================
# trading/signals/timeframe/single_tf.py
# ------------------------------------------------------------
# ✔ 単一時間足の健全性チェック
# ✔ トレンド / モメンタム / ボラの最低条件
# ✔ BUY / SELL 非依存
# ✔ entry_checker の前段フィルタ
# ============================================================

from typing import Dict

from trading.signals.factors import (
    trend,
    momentum,
    volatility,
)


# ============================================================
# 単一 TF フィルタ
# ============================================================

def check_single_tf(
    *,
    price: float,
    ma5: float,
    ma25: float,
    ma75: float,
    rsi: float,
    rci: float,
    atr: float,
) -> Dict[str, bool]:
    """
    単一時間足の状態をチェックする

    戻り値:
        {
            "trend_ok": bool,
            "momentum_ok": bool,
            "volatility_ok": bool,
            "tf_ok": bool
        }
    """

    result = {
        "trend_ok": False,
        "momentum_ok": False,
        "volatility_ok": False,
        "tf_ok": False,
    }

    # --- トレンド（MA 配列 or 方向性） ---
    if (
        trend.is_uptrend(ma_short=ma5, ma_mid=ma25, ma_long=ma75)
        or trend.is_downtrend(ma_short=ma5, ma_mid=ma25, ma_long=ma75)
    ):
        result["trend_ok"] = True

    # --- モメンタム（極端でないこと） ---
    if not momentum.rsi_overbought(rsi=rsi) and not momentum.rsi_oversold(rsi=rsi):
        result["momentum_ok"] = True

    # --- ボラティリティ（動いているか） ---
    if volatility.atr_ratio_above(
        atr=atr,
        price=price,
        ratio=0.002,  # 0.2% 以上
    ):
        result["volatility_ok"] = True

    # --- 総合 ---
    result["tf_ok"] = (
        result["trend_ok"]
        and result["momentum_ok"]
        and result["volatility_ok"]
    )

    return result


# ============================================================
# 軽量判定（True / False）
# ============================================================

def is_single_tf_ok(**kwargs) -> bool:
    """
    entry_checker 用の簡易版
    """
    result = check_single_tf(**kwargs)
    return result["tf_ok"]
