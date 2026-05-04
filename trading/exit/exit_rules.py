# ============================================================
# trading/exit/exit_rules.py
# Ver1.0.0-FINAL-PRICE-RULES
# ------------------------------------------------------------
# ✔ EXIT 価格計算専用
# ✔ 建値 / ATR トレール計算
# ✔ 純粋関数（副作用ゼロ）
# ✔ 状態判断は exit_state_machine に委譲
# ✔ ENTRY は STOP 前提
# ============================================================

from __future__ import annotations

from typing import Dict, Optional


# ============================================================
# パラメータ（最終確定）
# ============================================================

TRAIL_OFFSET_K = 0.25     # ATR に対するトレール幅


# ============================================================
# 建値ストップ
# ============================================================

def breakeven_stop(ctx: Dict) -> Optional[float]:
    """
    建値ストップ価格を返す

    return:
        entry_price or None
    """
    entry = ctx.get("entry_price")
    side = ctx.get("side")

    if entry is None or side not in ("BUY", "SELL"):
        return None

    # BUY / SELL ともに建値
    return float(entry)


# ============================================================
# ATR トレールストップ
# ============================================================

def calc_trailing_stop(ctx: Dict) -> Optional[float]:
    """
    ATR ベースのトレールストップ価格を計算

    BUY:
        stop = highest_price - ATR * K

    SELL:
        stop = lowest_price + ATR * K
    """

    atr = ctx.get("atr_1min", 0.0)
    side = ctx.get("side")

    if atr <= 0 or side not in ("BUY", "SELL"):
        return None

    offset = atr * TRAIL_OFFSET_K

    if side == "BUY":
        highest = ctx.get("highest")
        if highest is None:
            return None
        return float(highest) - offset

    else:
        lowest = ctx.get("lowest")
        if lowest is None:
            return None
        return float(lowest) + offset


# ============================================================
# 補助（将来拡張用）
# ============================================================

def clamp_stop_price(
    *,
    stop_price: float,
    entry_price: float,
    side: str,
) -> float:
    """
    ストップ価格の異常補正（安全装置）

    BUY:
        stop_price が entry_price を超えないようにする

    SELL:
        stop_price が entry_price を下回らないようにする
    """
    if side == "BUY":
        return min(stop_price, entry_price)
    else:
        return max(stop_price, entry_price)
