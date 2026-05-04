# ============================================================
# trading/signals/factors/momentum.py
# ------------------------------------------------------------
# ✔ モメンタム系の純粋因子
# ✔ RSI / RCI / Stochastic
# ✔ BUY / SELL 非依存
# ✔ AI 学習特徴量として再利用可能
# ============================================================

from typing import Optional


# ============================================================
# RSI
# ============================================================

def rsi_above(
    *,
    rsi: float,
    threshold: float = 50.0,
) -> bool:
    """
    RSI が指定値以上
    """
    if rsi is None:
        return False
    return rsi >= threshold


def rsi_below(
    *,
    rsi: float,
    threshold: float = 50.0,
) -> bool:
    """
    RSI が指定値以下
    """
    if rsi is None:
        return False
    return rsi <= threshold


def rsi_overbought(
    *,
    rsi: float,
    level: float = 70.0,
) -> bool:
    """
    RSI 買われ過ぎ
    """
    if rsi is None:
        return False
    return rsi >= level


def rsi_oversold(
    *,
    rsi: float,
    level: float = 30.0,
) -> bool:
    """
    RSI 売られ過ぎ
    """
    if rsi is None:
        return False
    return rsi <= level


# ============================================================
# RCI
# ============================================================

def rci_above(
    *,
    rci: float,
    threshold: float = 0.0,
) -> bool:
    """
    RCI が指定値以上
    """
    if rci is None:
        return False
    return rci >= threshold


def rci_below(
    *,
    rci: float,
    threshold: float = 0.0,
) -> bool:
    """
    RCI が指定値以下
    """
    if rci is None:
        return False
    return rci <= threshold


def rci_strong_up(
    *,
    rci: float,
    level: float = 80.0,
) -> bool:
    """
    RCI 強い上昇
    """
    if rci is None:
        return False
    return rci >= level


def rci_strong_down(
    *,
    rci: float,
    level: float = -80.0,
) -> bool:
    """
    RCI 強い下降
    """
    if rci is None:
        return False
    return rci <= level


# ============================================================
# ストキャスティクス（Slow）
# ============================================================

def stoch_golden_cross(
    *,
    prev_k: float,
    prev_d: float,
    k: float,
    d: float,
    lower_band: float = 20.0,
) -> bool:
    """
    ストキャス・ゴールデンクロス（主に売られ過ぎ圏）
    """
    if None in (prev_k, prev_d, k, d):
        return False

    return (
        prev_k <= prev_d
        and k > d
        and k <= lower_band
    )


def stoch_dead_cross(
    *,
    prev_k: float,
    prev_d: float,
    k: float,
    d: float,
    upper_band: float = 80.0,
) -> bool:
    """
    ストキャス・デッドクロス（主に買われ過ぎ圏）
    """
    if None in (prev_k, prev_d, k, d):
        return False

    return (
        prev_k >= prev_d
        and k < d
        and k >= upper_band
    )


def stoch_overbought(
    *,
    k: float,
    d: float,
    level: float = 80.0,
) -> bool:
    """
    ストキャス買われ過ぎ
    """
    if k is None or d is None:
        return False
    return k >= level and d >= level


def stoch_oversold(
    *,
    k: float,
    d: float,
    level: float = 20.0,
) -> bool:
    """
    ストキャス売られ過ぎ
    """
    if k is None or d is None:
        return False
    return k <= level and d <= level


# ============================================================
# モメンタム総合（軽量）
# ============================================================

def momentum_up(
    *,
    rsi: Optional[float],
    rci: Optional[float],
) -> bool:
    """
    軽量モメンタム上向き判定
    """
    if rsi is None or rci is None:
        return False

    return rsi >= 50 and rci >= 0


def momentum_down(
    *,
    rsi: Optional[float],
    rci: Optional[float],
) -> bool:
    """
    軽量モメンタム下向き判定
    """
    if rsi is None or rci is None:
        return False

    return rsi <= 50 and rci <= 0
