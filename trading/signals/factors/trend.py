# ============================================================
# trading/signals/factors/trend.py
# ------------------------------------------------------------
# ✔ トレンド系の純粋因子（BUY / SELL 非依存）
# ✔ MA / 傾き / ブレイク / レンジ判定
# ✔ single TF / multi TF どちらでも使用可
# ✔ AI 学習特徴量としても再利用可能
# ============================================================

from typing import Optional


# ============================================================
# 基本 MA トレンド
# ============================================================

def is_uptrend(
    *,
    ma_short: float,
    ma_mid: float,
    ma_long: Optional[float] = None,
) -> bool:
    """
    上昇トレンド判定
    MA5 > MA25 (> MA75)
    """
    if ma_short is None or ma_mid is None:
        return False

    if ma_long is None:
        return ma_short > ma_mid

    return ma_short > ma_mid > ma_long


def is_downtrend(
    *,
    ma_short: float,
    ma_mid: float,
    ma_long: Optional[float] = None,
) -> bool:
    """
    下降トレンド判定
    MA5 < MA25 (< MA75)
    """
    if ma_short is None or ma_mid is None:
        return False

    if ma_long is None:
        return ma_short < ma_mid

    return ma_short < ma_mid < ma_long


# ============================================================
# MA クロス
# ============================================================

def golden_cross(
    *,
    prev_ma_short: float,
    prev_ma_mid: float,
    ma_short: float,
    ma_mid: float,
) -> bool:
    """
    ゴールデンクロス（短期が中期を上抜け）
    """
    if None in (prev_ma_short, prev_ma_mid, ma_short, ma_mid):
        return False

    return prev_ma_short <= prev_ma_mid and ma_short > ma_mid


def dead_cross(
    *,
    prev_ma_short: float,
    prev_ma_mid: float,
    ma_short: float,
    ma_mid: float,
) -> bool:
    """
    デッドクロス（短期が中期を下抜け）
    """
    if None in (prev_ma_short, prev_ma_mid, ma_short, ma_mid):
        return False

    return prev_ma_short >= prev_ma_mid and ma_short < ma_mid


# ============================================================
# MA 傾き（トレンドの強さ）
# ============================================================

def ma_slope_up(
    *,
    ma_prev: float,
    ma_now: float,
    threshold: float = 0.0,
) -> bool:
    """
    MA が上向きか
    threshold を入れると弱いノイズを除外
    """
    if ma_prev is None or ma_now is None:
        return False

    return (ma_now - ma_prev) > threshold


def ma_slope_down(
    *,
    ma_prev: float,
    ma_now: float,
    threshold: float = 0.0,
) -> bool:
    """
    MA が下向きか
    """
    if ma_prev is None or ma_now is None:
        return False

    return (ma_prev - ma_now) > threshold


# ============================================================
# 価格位置（トレンド内か）
# ============================================================

def price_above_ma(price: float, ma: float) -> bool:
    """
    現在値が MA より上
    """
    if price is None or ma is None:
        return False
    return price > ma


def price_below_ma(price: float, ma: float) -> bool:
    """
    現在値が MA より下
    """
    if price is None or ma is None:
        return False
    return price < ma


# ============================================================
# ブレイク / 押し目 / 戻り
# ============================================================

def break_above_high(
    *,
    price: float,
    prev_high: float,
) -> bool:
    """
    直近高値ブレイク
    """
    if price is None or prev_high is None:
        return False
    return price > prev_high


def break_below_low(
    *,
    price: float,
    prev_low: float,
) -> bool:
    """
    直近安値ブレイク
    """
    if price is None or prev_low is None:
        return False
    return price < prev_low


def pullback_to_ma(
    *,
    low: float,
    ma: float,
    tolerance: float = 0.002,
) -> bool:
    """
    押し目判定（MA 付近まで下げている）
    tolerance = 0.2% など
    """
    if low is None or ma is None:
        return False

    return abs(low - ma) / ma <= tolerance


def rebound_from_ma(
    *,
    prev_low: float,
    low: float,
    ma: float,
) -> bool:
    """
    MA 反発（前回より安値を切っていない）
    """
    if None in (prev_low, low, ma):
        return False

    return low > prev_low and low >= ma


# ============================================================
# レンジ判定（トレンド不在）
# ============================================================

def is_range(
    *,
    ma_short: float,
    ma_mid: float,
    threshold: float = 0.001,
) -> bool:
    """
    MA が収束している（レンジ）
    """
    if ma_short is None or ma_mid is None:
        return False

    return abs(ma_short - ma_mid) / ma_mid <= threshold
