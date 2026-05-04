# ============================================================
# trading/signals/rules/sell_rules.py
# ------------------------------------------------------------
# ✔ SELL（SHORT）エントリー用ルール集
# ✔ factors を組み合わせるだけ（状態管理なし）
# ✔ entry_checker から呼ばれる想定
# ✔ ログ理由・スコア化を前提
# ============================================================

from typing import Tuple, List

from trading.signals.factors import (
    trend,
    momentum,
    volume,
    pattern,
    volatility,
)

# ============================================================
# SELL ルール結果型
# ============================================================

RuleResult = Tuple[bool, List[str]]  # (is_sell, reasons)


# ============================================================
# トレンドフォロー SELL
# ============================================================

def sell_trend_follow(
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
    王道下降トレンドフォロー SELL
    """
    reasons: List[str] = []

    # --- トレンド ---
    if trend.is_downtrend(ma_short=ma5, ma_mid=ma25, ma_long=ma75):
        reasons.append("下降トレンド(MA)")
    else:
        return False, reasons

    # --- モメンタム ---
    if momentum.momentum_down(rsi=rsi, rci=rci):
        reasons.append("モメンタム下向き")
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
    if volume.price_below_vwap(price=price, vwap=vwap):
        reasons.append("VWAP下")
    else:
        return False, reasons

    return True, reasons


# ============================================================
# 戻り売り SELL
# ============================================================

def sell_pullback(
    *,
    high: float,
    ma25: float,
    ma75: float,
    rsi: float,
    volume_now: float,
    volume_avg: float,
) -> RuleResult:
    """
    戻り売り SELL（MA25 付近）
    """
    reasons: List[str] = []

    # --- 中期下降トレンド ---
    if not trend.is_downtrend(ma_short=ma25, ma_mid=ma75):
        return False, reasons

    # --- 戻り ---
    if trend.pullback_to_ma(low=high, ma=ma25, tolerance=0.003):
        reasons.append("MA25戻り")
    else:
        return False, reasons

    # --- 買われすぎ回避 ---
    if not momentum.rsi_overbought(rsi=rsi, level=70):
        reasons.append("買われすぎ回避")
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
# 安値ブレイク SELL
# ============================================================

def sell_breakdown(
    *,
    price: float,
    prev_low: float,
    volume_now: float,
    volume_avg: float,
    atr: float,
    price_for_atr: float,
) -> RuleResult:
    """
    安値ブレイク SELL
    """
    reasons: List[str] = []

    # --- 安値ブレイク ---
    if trend.break_below_low(price=price, prev_low=prev_low):
        reasons.append("安値ブレイク")
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
# ローソク足反転 SELL
# ============================================================

def sell_reversal_pattern(
    *,
    prev_open: float,
    prev_close: float,
    open_: float,
    close: float,
    high: float,
    ma25: float,
) -> RuleResult:
    """
    ローソク足反転 SELL
    """
    reasons: List[str] = []

    # --- 包み足 or シューティングスター ---
    if (
        pattern.bearish_engulfing(
            prev_open=prev_open,
            prev_close=prev_close,
            open_=open_,
            close=close,
        )
        or pattern.shooting_star(
            open_=open_,
            close=close,
            high=high,
            low=min(open_, close),
        )
    ):
        reasons.append("反転ローソク足")
    else:
        return False, reasons

    # --- MA25 以下 ---
    if close <= ma25:
        reasons.append("MA25下")
    else:
        return False, reasons

    return True, reasons


# ============================================================
# SELL ルール統合（entry_checker 用）
# ============================================================

def check_sell_rules(**kwargs) -> RuleResult:
    """
    SELL ルール総合判定
    上から順に評価し、最初に成立したものを採用
    """

    rule_funcs = [
        sell_trend_follow,
        sell_pullback,
        sell_breakdown,
        sell_reversal_pattern,
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
