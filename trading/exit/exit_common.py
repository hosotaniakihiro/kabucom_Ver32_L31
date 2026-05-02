# ============================================================
# trading/exit/exit_common.py
# ------------------------------------------------------------
# EXIT 共通ロジック
# ・上位足環境判定
# ・EXIT加速係数
# ・動的トレーリング幅
# ============================================================

from __future__ import annotations
import datetime as dt
from typing import Dict, Any

from global_state import global_data
from trading.exit.exit_context import ExitContext


def safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


# ============================================================
# 上位足方向判定（3m / 5m）
# ============================================================

def judge_direction_from_summary(row: Dict[str, Any], side: str) -> str:
    if not row:
        return "FLAT"

    close = safe_float(row.get("close_price"))
    ma25  = safe_float(row.get("ma25"))
    ma75  = safe_float(row.get("ma75"))

    if close > ma25 > ma75:
        trend = "UP"
    elif close < ma25 < ma75:
        trend = "DOWN"
    else:
        return "FLAT"

    if side == "BUY" and trend == "UP":
        return "ALIGN"
    if side == "SELL" and trend == "DOWN":
        return "ALIGN"

    return "AGAINST"


# ============================================================
# EXIT加速係数
# ============================================================

def calc_exit_accel(*, ctx: ExitContext, side: str, now: dt.datetime) -> float:
    s3 = global_data.summary_by_interval.get("3min", {}).get(ctx.symbol)
    s5 = global_data.summary_by_interval.get("5min", {}).get(ctx.symbol)

    env3 = judge_direction_from_summary(s3, side)
    env5 = judge_direction_from_summary(s5, side)

    if env3 == env5 == "ALIGN":
        base = 0.8
    elif "AGAINST" in (env3, env5):
        base = 1.8
    else:
        base = 1.0

    pnl = ctx.unrealized_pnl
    if pnl <= 0:
        pnl_factor = 1.3
    elif pnl < ctx.mfe * 0.3:
        pnl_factor = 1.0
    else:
        pnl_factor = 0.6

    hold_sec = ctx.holding_seconds(now)
    if hold_sec < 30:
        time_factor = 1.0
    elif hold_sec < 90:
        time_factor = 1.1
    else:
        time_factor = 1.4

    accel = base * pnl_factor * time_factor
    accel = max(0.6, min(accel, 2.5))

    ctx.exit_accel = accel
    ctx.exit_env = f"{env3}/{env5}"
    return accel


# ============================================================
# 動的トレーリング幅
# ============================================================

def calc_dynamic_trail_pct(
    *,
    ctx: ExitContext,
    base_trail: float = 0.003,
    trail_min: float = 0.0015,
    trail_max: float = 0.01,
) -> float:
    profit_factor = min(1.0 + ctx.mfe_pct * 2.0, 3.0)
    accel = getattr(ctx, "exit_accel", 1.0)
    accel_factor = 1.0 / max(0.7, accel)

    trail = base_trail * profit_factor * accel_factor
    return max(trail_min, min(trail, trail_max))
