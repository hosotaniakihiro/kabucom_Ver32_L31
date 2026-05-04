# ============================================================
# trading/signals/factors/pattern.py
# ------------------------------------------------------------
# ✔ ローソク足パターン系の純粋因子
# ✔ 単一足 / 2本足 / 連続足対応
# ✔ BUY / SELL 非依存
# ✔ AI 学習特徴量として再利用可能
# ============================================================

from typing import Optional


# ============================================================
# 基本ユーティリティ
# ============================================================

def is_bullish(open_: float, close: float) -> bool:
    """
    陽線か
    """
    if open_ is None or close is None:
        return False
    return close > open_


def is_bearish(open_: float, close: float) -> bool:
    """
    陰線か
    """
    if open_ is None or close is None:
        return False
    return close < open_


def body_size(open_: float, close: float) -> float:
    """
    実体サイズ
    """
    if open_ is None or close is None:
        return 0.0
    return abs(close - open_)


def upper_shadow(high: float, open_: float, close: float) -> float:
    """
    上ヒゲ長
    """
    if None in (high, open_, close):
        return 0.0
    return high - max(open_, close)


def lower_shadow(low: float, open_: float, close: float) -> float:
    """
    下ヒゲ長
    """
    if None in (low, open_, close):
        return 0.0
    return min(open_, close) - low


# ============================================================
# 単一足パターン
# ============================================================

def doji(
    *,
    open_: float,
    close: float,
    high: float,
    low: float,
    tolerance: float = 0.1,
) -> bool:
    """
    ドージー（実体が非常に小さい）
    tolerance = 実体 / (高値-安値)
    """
    if None in (open_, close, high, low):
        return False

    total_range = high - low
    if total_range <= 0:
        return False

    return body_size(open_, close) / total_range <= tolerance


def hammer(
    *,
    open_: float,
    close: float,
    high: float,
    low: float,
    ratio: float = 2.0,
) -> bool:
    """
    ハンマー（下ヒゲが実体の ratio 倍以上）
    """
    if None in (open_, close, high, low):
        return False

    body = body_size(open_, close)
    if body == 0:
        return False

    return (
        lower_shadow(low, open_, close) >= body * ratio
        and upper_shadow(high, open_, close) <= body
    )


def shooting_star(
    *,
    open_: float,
    close: float,
    high: float,
    low: float,
    ratio: float = 2.0,
) -> bool:
    """
    シューティングスター（上ヒゲが実体の ratio 倍以上）
    """
    if None in (open_, close, high, low):
        return False

    body = body_size(open_, close)
    if body == 0:
        return False

    return (
        upper_shadow(high, open_, close) >= body * ratio
        and lower_shadow(low, open_, close) <= body
    )


# ============================================================
# 2本足パターン
# ============================================================

def bullish_engulfing(
    *,
    prev_open: float,
    prev_close: float,
    open_: float,
    close: float,
) -> bool:
    """
    包み足（強気）
    """
    if None in (prev_open, prev_close, open_, close):
        return False

    return (
        is_bearish(prev_open, prev_close)
        and is_bullish(open_, close)
        and open_ <= prev_close
        and close >= prev_open
    )


def bearish_engulfing(
    *,
    prev_open: float,
    prev_close: float,
    open_: float,
    close: float,
) -> bool:
    """
    包み足（弱気）
    """
    if None in (prev_open, prev_close, open_, close):
        return False

    return (
        is_bullish(prev_open, prev_close)
        and is_bearish(open_, close)
        and open_ >= prev_close
        and close <= prev_open
    )


def harami_bullish(
    *,
    prev_open: float,
    prev_close: float,
    open_: float,
    close: float,
) -> bool:
    """
    はらみ足（強気）
    """
    if None in (prev_open, prev_close, open_, close):
        return False

    return (
        is_bearish(prev_open, prev_close)
        and is_bullish(open_, close)
        and open_ >= prev_close
        and close <= prev_open
    )


def harami_bearish(
    *,
    prev_open: float,
    prev_close: float,
    open_: float,
    close: float,
) -> bool:
    """
    はらみ足（弱気）
    """
    if None in (prev_open, prev_close, open_, close):
        return False

    return (
        is_bullish(prev_open, prev_close)
        and is_bearish(open_, close)
        and open_ <= prev_close
        and close >= prev_open
    )


# ============================================================
# 連続足パターン（簡易）
# ============================================================

def three_white_soldiers(
    *,
    c1_open: float,
    c1_close: float,
    c2_open: float,
    c2_close: float,
    c3_open: float,
    c3_close: float,
) -> bool:
    """
    三兵（上昇）
    """
    if None in (
        c1_open, c1_close,
        c2_open, c2_close,
        c3_open, c3_close,
    ):
        return False

    return (
        is_bullish(c1_open, c1_close)
        and is_bullish(c2_open, c2_close)
        and is_bullish(c3_open, c3_close)
        and c2_open > c1_open
        and c3_open > c2_open
    )


def three_black_crows(
    *,
    c1_open: float,
    c1_close: float,
    c2_open: float,
    c2_close: float,
    c3_open: float,
    c3_close: float,
) -> bool:
    """
    三羽烏（下落）
    """
    if None in (
        c1_open, c1_close,
        c2_open, c2_close,
        c3_open, c3_close,
    ):
        return False

    return (
        is_bearish(c1_open, c1_close)
        and is_bearish(c2_open, c2_close)
        and is_bearish(c3_open, c3_close)
        and c2_open < c1_open
        and c3_open < c2_open
    )
