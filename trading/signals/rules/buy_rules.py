# ============================================================
# trading/signals/rules/buy_rules.py
# ------------------------------------------------------------
# ✔ BUY（LONG）エントリー用ルール集
# ✔ factors を組み合わせるだけ（状態管理なし）
# ✔ entry_checker から呼ばれる想定
# ✔ ログ理由・スコア化を前提
# ============================================================

from typing import Dict, Tuple, List

from trading.signals.factors import (
    trend,
    momentum,
    volume,
    pattern,
    volatility,
)


# ============================================================
# BUY ルール結果型
# ============================================================

RuleResult = Tuple[bool, List[str]]  # (is_buy, reasons)


# ============================================================
# 基本 BUY トレンドルール
# ============================================================

def buy_trend_follow(
    *,
    price: float,
    ma5: float,
    ma25: float,
    ma75: float,
    rsi: float,
    rci: float,
    volume_now: float,
    volume_avg: float,
    vwap: float,
) -> RuleResult:
    """
    王道トレンドフォロー BUY
    """
    reasons: List[str] = []

    # --- トレンド ---
    if trend.is_uptrend(ma_short=ma5, ma_mid=ma25, ma_long=ma75):
        reasons.append("上昇トレンド(MA)")
    else:
        return False, reasons

    # --- モメンタム ---
    if momentum.momentum_up(rsi=rsi, rci=rci):
        reasons.append("モメンタム上向き")
    else:
        return False, reasons

    # --- 出来高 ---
    if volume.volume_spike(
        volume=volume_now,
        avg_volume=volume_avg,
        ratio=1.3,
    ):
        reasons.append("出来高増加")
    else:
        return False, reasons

    # --- VWAP ---
    if volume.price_above_vwap(price=price, vwap=vwap):
        reasons.append("VWAP上")
    else:
        return False, reasons

    return True, reasons


# ============================================================
# 押し目 BUY
# ============================================================

def buy_pullback(
    *,
    low: float,
    ma25: float,
    ma75: float,
    rsi: float,
    volume_now: float,
    volume_avg: float,
) -> RuleResult:
    """
    押し目 BUY（MA25 付近）
    """
    reasons: List[str] = []

    # --- 中期トレンド維持 ---
    if not trend.is_uptrend(ma_short=ma25, ma_mid=ma75):
        return False, reasons

    # --- 押し目 ---
    if trend.pullback_to_ma(low=low, ma=ma25, tolerance=0.003):
        reasons.append("MA25押し目")
    else:
        return False, reasons

    # --- 売られすぎ回避 ---
    if not momentum.rsi_oversold(rsi=rsi, level=30):
        reasons.append("売られすぎ回避")
    else:
        return False, reasons

    # --- 出来高 ---
    if volume.volume_contraction(
        volume=volume_now,
        avg_volume=volume_avg,
        ratio=0.8,
    ):
        reasons.append("出来高収縮")
    else:
        return False, reasons

    return True, reasons


# ============================================================
# ブレイクアウト BUY
# ============================================================

def buy_breakout(
    *,
    price: float,
    prev_high: float,
    volume_now: float,
    volume_avg: float,
    atr: float,
    price_for_atr: float,
) -> RuleResult:
    """
    高値ブレイク BUY
    """
    reasons: List[str] = []

    # --- 高値ブレイク ---
    if trend.break_above_high(price=price, prev_high=prev_high):
        reasons.append("高値ブレイク")
    else:
        return False, reasons

    # --- 出来高 ---
    if volume.volume_spike(
        volume=volume_now,
        avg_volume=volume_avg,
        ratio=1.5,
    ):
        reasons.append("出来高急増")
    else:
        return False, reasons

    # --- ボラ確認 ---
    if volatility.atr_ratio_above(
        atr=atr,
        price=price_for_atr,
        ratio=0.004,
    ):
        reasons.append("十分な値幅(ATR)")
    else:
        return False, reasons

    return True, reasons


# ============================================================
# ローソク足反転 BUY
# ============================================================

def buy_reversal_pattern(
    *,
    prev_open: float,
    prev_close: float,
    open_: float,
    close: float,
    low: float,
    ma25: float,
) -> RuleResult:
    """
    ローソク足反転 BUY
    """
    reasons: List[str] = []

    # --- 包み足 or ハンマー ---
    if (
        pattern.bullish_engulfing(
            prev_open=prev_open,
            prev_close=prev_close,
            open_=open_,
            close=close,
        )
        or pattern.hammer(
            open_=open_,
            close=close,
            high=max(open_, close),
            low=low,
        )
    ):
        reasons.append("反転ローソク足")
    else:
        return False, reasons

    # --- MA25 以上 ---
    if close >= ma25:
        reasons.append("MA25上")
    else:
        return False, reasons

    return True, reasons


# ============================================================
# BUY ルール統合（entry_checker 用）
# ============================================================

def check_buy_rules(**kwargs) -> RuleResult:
    """
    BUY ルール総合判定
    上から順に評価し、最初に成立したものを採用
    """

    rule_funcs = [
        buy_trend_follow,
        buy_pullback,
        buy_breakout,
        buy_reversal_pattern,
    ]

    for rule in rule_funcs:
        try:
            ok, reasons = rule(**kwargs)
            if ok:
                return True, reasons
        except TypeError:
            # 必要な引数が足りない場合はスキップ
            continue

    return False, []
