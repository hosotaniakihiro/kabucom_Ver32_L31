# ============================================================
# trading/signals/factors/volatility.py
# ------------------------------------------------------------
# ✔ ボラティリティ系の純粋因子
# ✔ ATR / Bollinger Bands / レンジ判定
# ✔ BUY / SELL 非依存
# ✔ EXIT / ポジションサイズ / AI 学習にも再利用可能
# ============================================================

from typing import Optional


# ============================================================
# ATR（Average True Range）
# ============================================================

def atr_above(
    *,
    atr: float,
    threshold: float,
) -> bool:
    """
    ATR が指定値以上（十分な値動き）
    """
    if atr is None:
        return False
    return atr >= threshold


def atr_below(
    *,
    atr: float,
    threshold: float,
) -> bool:
    """
    ATR が指定値以下（低ボラ・様子見）
    """
    if atr is None:
        return False
    return atr <= threshold


def atr_ratio_above(
    *,
    atr: float,
    price: float,
    ratio: float = 0.005,
) -> bool:
    """
    ATR / 価格 比率でのボラ判定
    ratio = 0.5% など
    """
    if atr is None or price is None or price == 0:
        return False

    return (atr / price) >= ratio


def atr_ratio_below(
    *,
    atr: float,
    price: float,
    ratio: float = 0.003,
) -> bool:
    """
    ATR / 価格 比率が低い（レンジ）
    """
    if atr is None or price is None or price == 0:
        return False

    return (atr / price) <= ratio


# ============================================================
# ボリンジャーバンド
# ============================================================

def price_above_bb_upper(
    *,
    price: float,
    bb_upper: float,
) -> bool:
    """
    価格が BB 上限を上抜け
    """
    if price is None or bb_upper is None:
        return False
    return price > bb_upper


def price_below_bb_lower(
    *,
    price: float,
    bb_lower: float,
) -> bool:
    """
    価格が BB 下限を下抜け
    """
    if price is None or bb_lower is None:
        return False
    return price < bb_lower


def bb_band_width(
    *,
    bb_upper: float,
    bb_lower: float,
    mid: Optional[float] = None,
) -> Optional[float]:
    """
    BB バンド幅（正規化可能）
    """
    if bb_upper is None or bb_lower is None:
        return None

    width = bb_upper - bb_lower
    if mid and mid != 0:
        return width / mid

    return width


def bb_squeeze(
    *,
    bb_upper: float,
    bb_lower: float,
    mid: Optional[float] = None,
    threshold: float = 0.01,
) -> bool:
    """
    ボリンジャーバンド収縮（スクイーズ）
    threshold = 正規化後の幅
    """
    width = bb_band_width(bb_upper=bb_upper, bb_lower=bb_lower, mid=mid)
    if width is None:
        return False

    return width <= threshold


def bb_expansion(
    *,
    bb_upper: float,
    bb_lower: float,
    mid: Optional[float] = None,
    threshold: float = 0.02,
) -> bool:
    """
    ボリンジャーバンド拡張
    """
    width = bb_band_width(bb_upper=bb_upper, bb_lower=bb_lower, mid=mid)
    if width is None:
        return False

    return width >= threshold


# ============================================================
# ボラティリティ複合
# ============================================================

def high_volatility(
    *,
    atr: float,
    price: float,
    bb_upper: Optional[float] = None,
    bb_lower: Optional[float] = None,
) -> bool:
    """
    高ボラ判定（ATR or BB）
    """
    if atr_ratio_above(atr=atr, price=price):
        return True

    if bb_upper and bb_lower:
        return bb_expansion(
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            mid=price,
        )

    return False


def low_volatility(
    *,
    atr: float,
    price: float,
    bb_upper: Optional[float] = None,
    bb_lower: Optional[float] = None,
) -> bool:
    """
    低ボラ判定（様子見）
    """
    if atr_ratio_below(atr=atr, price=price):
        return True

    if bb_upper and bb_lower:
        return bb_squeeze(
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            mid=price,
        )

    return False
