# ============================================================
# File: trading/filters/top10_turnover_filter.py
# ============================================================
# 定時サマリー TOP10 用 売買代金フィルター（完成版・段階制対応）
# ------------------------------------------------------------
# ✔ 寄り付きは 7億円ペース換算で判定
# ✔ 前場中盤は出来高急減を排除
# ✔ 昼以降は実績累計で締める
# ✔ BUY / SELL 共通（TOP10 表示品質保証）
# ✔ ★ relax=True で昼・閑散時間帯を安全に緩和（NEW）
# ============================================================

from __future__ import annotations
import datetime as dt

MARKET_MINUTES = 390


# ------------------------------------------------------------
# 売買代金ペース換算
# ------------------------------------------------------------

def projected_daily_turnover(current_turnover: float, now: dt.time) -> float:
    minutes = (now.hour * 60 + now.minute) - (9 * 60)
    minutes = max(minutes, 1)
    return current_turnover / minutes * MARKET_MINUTES


# ------------------------------------------------------------
# 出来高急減判定（前場中盤用）
# ------------------------------------------------------------

def has_volume_drop(turnover_5m: float, turnover_prev15m_avg: float) -> bool:
    if turnover_prev15m_avg <= 0:
        return False
    return (turnover_5m / turnover_prev15m_avg) < 0.5


# ------------------------------------------------------------
# TOP10 最終フィルター（段階制対応）
# ------------------------------------------------------------

def passes_top10_filter(
    *,
    daily_turnover: float,
    turnover_5m: float,
    turnover_prev15m_avg: float,
    now: dt.time,
    cfg,
    relax: bool = False,
) -> bool:
    """
    Args:
        relax:
            False = 通常条件（本命）
            True  = 昼・閑散用の緩和条件（全滅防止）
    """

    # ----------------------------
    # 売買代金条件（時間帯別）
    # ----------------------------
    if now < dt.time(10, 30):
        threshold = cfg.MIN_PROJECTED_TURNOVER
        if relax:
            threshold *= 0.5

        if projected_daily_turnover(daily_turnover, now) < threshold:
            return False

    elif now < dt.time(12, 30):
        threshold = cfg.MIN_MIDDAY_TURNOVER
        if relax:
            threshold *= 0.5

        if daily_turnover < threshold:
            return False

    else:
        threshold = cfg.MIN_DAILY_TURNOVER
        if relax:
            threshold *= 0.5

        if daily_turnover < threshold:
            return False

    # ----------------------------
    # 前場中盤のみ：出来高急減を排除
    # （※緩和時もこの条件は維持）
    # ----------------------------
    if dt.time(9, 15) <= now < dt.time(10, 30):
        if has_volume_drop(turnover_5m, turnover_prev15m_avg):
            return False

    return True
