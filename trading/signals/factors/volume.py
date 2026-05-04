# ============================================================
# trading/signals/factors/volume.py
# ------------------------------------------------------------
# ✔ 出来高・売買代金・VWAP 系の純粋因子
# ✔ PUSH / 1m / 3m / 5m 共通利用
# ✔ BUY / SELL 非依存
# ✔ AI 学習特徴量として再利用可能
# ============================================================

from typing import Optional


# ============================================================
# 出来高
# ============================================================

def volume_above(
    *,
    volume: float,
    threshold: float,
) -> bool:
    """
    出来高が指定値以上
    """
    if volume is None:
        return False
    return volume >= threshold


def volume_spike(
    *,
    volume: float,
    avg_volume: float,
    ratio: float = 1.5,
) -> bool:
    """
    出来高急増判定
    volume >= avg_volume * ratio
    """
    if volume is None or avg_volume is None or avg_volume == 0:
        return False

    return volume >= avg_volume * ratio


def volume_contraction(
    *,
    volume: float,
    avg_volume: float,
    ratio: float = 0.7,
) -> bool:
    """
    出来高収縮（押し目・レンジ判定用）
    """
    if volume is None or avg_volume is None or avg_volume == 0:
        return False

    return volume <= avg_volume * ratio


# ============================================================
# 売買代金
# ============================================================

def turnover_above(
    *,
    turnover: float,
    threshold: float,
) -> bool:
    """
    売買代金が指定値以上
    """
    if turnover is None:
        return False
    return turnover >= threshold


# ============================================================
# VWAP
# ============================================================

def price_above_vwap(
    *,
    price: float,
    vwap: float,
) -> bool:
    """
    現在値が VWAP より上
    """
    if price is None or vwap is None:
        return False
    return price > vwap


def price_below_vwap(
    *,
    price: float,
    vwap: float,
) -> bool:
    """
    現在値が VWAP より下
    """
    if price is None or vwap is None:
        return False
    return price < vwap


def vwap_support(
    *,
    low: float,
    vwap: float,
    tolerance: float = 0.001,
) -> bool:
    """
    VWAP 付近で下げ止まり（支持）
    tolerance = 0.1%〜0.3% 程度
    """
    if low is None or vwap is None:
        return False

    return abs(low - vwap) / vwap <= tolerance


def vwap_resistance(
    *,
    high: float,
    vwap: float,
    tolerance: float = 0.001,
) -> bool:
    """
    VWAP 付近で上値抑制（抵抗）
    """
    if high is None or vwap is None:
        return False

    return abs(high - vwap) / vwap <= tolerance


# ============================================================
# 出来高 × VWAP 複合
# ============================================================

def volume_breakout_above_vwap(
    *,
    price: float,
    vwap: float,
    volume: float,
    avg_volume: float,
    ratio: float = 1.5,
) -> bool:
    """
    出来高を伴って VWAP を上抜け
    """
    return (
        price_above_vwap(price=price, vwap=vwap)
        and volume_spike(volume=volume, avg_volume=avg_volume, ratio=ratio)
    )


def volume_breakdown_below_vwap(
    *,
    price: float,
    vwap: float,
    volume: float,
    avg_volume: float,
    ratio: float = 1.5,
) -> bool:
    """
    出来高を伴って VWAP を下抜け
    """
    return (
        price_below_vwap(price=price, vwap=vwap)
        and volume_spike(volume=volume, avg_volume=avg_volume, ratio=ratio)
    )
