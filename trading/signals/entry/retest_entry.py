# ============================================================
# trading/signals/entry/retest_entry.py
# ------------------------------------------------------------
# ✔ ブレイク後の「再テスト（Retest）」での再エントリー判定
# ✔ 価格×出来高×モメンタム×反転足 を総合
# ✔ BUY / SELL 非依存（方向は引数で指定）
# ✔ state / 注文ロジックには触れない
# ============================================================

from typing import Dict, Tuple, List, Optional

from trading.signals.factors import (
    trend,
    momentum,
    volume,
    pattern,
)

# ============================================================
# 型
# ============================================================

RetestResult = Tuple[bool, List[str]]  # (ok, reasons)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _near_level(
    *,
    price: float,
    level: float,
    tolerance: float,
) -> bool:
    """
    価格が level 付近にあるか
    tolerance: 比率（0.1%〜0.5%）
    """
    if price is None or level is None or level == 0:
        return False
    return abs(price - level) / level <= tolerance


# ============================================================
# BUY（上方向）Retest
# ============================================================

def retest_buy(
    *,
    price: float,
    low: float,
    close: float,
    prev_high: float,
    ma25: Optional[float],
    vwap: Optional[float],
    rsi: float,
    rci: float,
    volume_now: float,
    volume_avg: float,
    prev_open: Optional[float] = None,
    prev_close: Optional[float] = None,
    tolerance: float = 0.003,
) -> RetestResult:
    """
    上昇ブレイク後の再テスト BUY
    """

    reasons: List[str] = []

    # --- 再テスト水準（優先度順） ---
    retest_levels = []

    if prev_high:
        retest_levels.append(("直近高値", prev_high))
    if ma25:
        retest_levels.append(("MA25", ma25))
    if vwap:
        retest_levels.append(("VWAP", vwap))

    # --- 水準タッチ確認 ---
    hit = False
    for name, level in retest_levels:
        if _near_level(price=low, level=level, tolerance=tolerance):
            reasons.append(f"{name}再テスト")
            hit = True
            break

    if not hit:
        return False, reasons

    # --- 反発条件 ---
    if close <= price:
        return False, reasons

    # --- モメンタム ---
    if not momentum.momentum_up(rsi=rsi, rci=rci):
        return False, reasons
    reasons.append("モメンタム上向き")

    # --- 出来高（急減していない） ---
    if not volume.volume_contraction(
        volume=volume_now,
        avg_volume=volume_avg,
        ratio=0.9,
    ):
        reasons.append("出来高維持")
    else:
        # 押し目での急減はOKだが、完全枯れはNG
        pass

    # --- 反転足（任意） ---
    if prev_open is not None and prev_close is not None:
        if pattern.bullish_engulfing(
            prev_open=prev_open,
            prev_close=prev_close,
            open_=price,
            close=close,
        ):
            reasons.append("反転包み足")

    return True, reasons


# ============================================================
# SELL（下方向）Retest
# ============================================================

def retest_sell(
    *,
    price: float,
    high: float,
    close: float,
    prev_low: float,
    ma25: Optional[float],
    vwap: Optional[float],
    rsi: float,
    rci: float,
    volume_now: float,
    volume_avg: float,
    prev_open: Optional[float] = None,
    prev_close: Optional[float] = None,
    tolerance: float = 0.003,
) -> RetestResult:
    """
    下落ブレイク後の再テスト SELL
    """

    reasons: List[str] = []

    # --- 再テスト水準 ---
    retest_levels = []

    if prev_low:
        retest_levels.append(("直近安値", prev_low))
    if ma25:
        retest_levels.append(("MA25", ma25))
    if vwap:
        retest_levels.append(("VWAP", vwap))

    # --- 水準タッチ確認 ---
    hit = False
    for name, level in retest_levels:
        if _near_level(price=high, level=level, tolerance=tolerance):
            reasons.append(f"{name}再テスト")
            hit = True
            break

    if not hit:
        return False, reasons

    # --- 反落条件 ---
    if close >= price:
        return False, reasons

    # --- モメンタム ---
    if not momentum.momentum_down(rsi=rsi, rci=rci):
        return False, reasons
    reasons.append("モメンタム下向き")

    # --- 出来高 ---
    if not volume.volume_contraction(
        volume=volume_now,
        avg_volume=volume_avg,
        ratio=0.9,
    ):
        reasons.append("出来高維持")

    # --- 反転足（任意） ---
    if prev_open is not None and prev_close is not None:
        if pattern.bearish_engulfing(
            prev_open=prev_open,
            prev_close=prev_close,
            open_=price,
            close=close,
        ):
            reasons.append("反転包み足")

    return True, reasons


# ============================================================
# entry_checker 用ラッパ
# ============================================================

def check_retest(
    *,
    side: str,
    **kwargs,
) -> RetestResult:
    """
    side: "BUY" or "SELL"
    """
    if side == "BUY":
        return retest_buy(**kwargs)
    if side == "SELL":
        return retest_sell(**kwargs)

    return False, []
