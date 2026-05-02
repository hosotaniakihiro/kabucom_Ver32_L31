# ============================================================
# trading/exit/exit_ranking_strategy.py
# ------------------------------------------------------------
# RANKING 専用 EXIT 戦略
# ・利益ゾーン
# ・動的トレーリング
# ============================================================

from __future__ import annotations
import datetime as dt
from typing import Optional

from trading.exit.exit_context import ExitContext
from trading.exit.exit_common import calc_exit_accel, calc_dynamic_trail_pct

# 利益ゾーン突入ライン
RANKING_PROFIT_ZONE = 0.004   # +0.4%


def should_exit_ranking(
    *,
    ctx: ExitContext,
    price: float,
    side: str,
    now: dt.datetime,
) -> Optional[str]:
    """
    return: exit_reason or None
    """

    ret = ctx.profit_pct(price)

    # 利益ゾーン突入（初回のみ）
    if ret >= RANKING_PROFIT_ZONE:
        ctx.in_profit_zone = True

    if not getattr(ctx, "in_profit_zone", False):
        return None

    # accel を更新（動的幅に使う）
    calc_exit_accel(ctx=ctx, side=side, now=now)

    trail_pct = calc_dynamic_trail_pct(ctx=ctx)

    if side == "BUY":
        peak_price = ctx.entry_price * (1 + ctx.mfe_pct)
        drawdown = (peak_price - price) / peak_price
        if drawdown >= trail_pct:
            return "RANKING_DYNAMIC_TRAIL"

    else:  # SELL
        trough_price = ctx.entry_price * (1 - ctx.mfe_pct)
        drawup = (price - trough_price) / trough_price
        if drawup >= trail_pct:
            return "RANKING_DYNAMIC_TRAIL"

    return None
