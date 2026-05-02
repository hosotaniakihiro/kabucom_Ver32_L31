# ============================================================
# File   : trading/exit/risk_parity_exit.py
# Ver1.0-FINAL-RISK-PARITY-EXIT
# ------------------------------------------------------------
# ✔ ENTRY と完全対称の Risk Parity EXIT
# ✔ ATR × confidence による損切り / 利確
# ✔ 感覚的ルールを完全排除
# ✔ 副作用ゼロ（計算のみ）
# ============================================================

from __future__ import annotations

import math
from config import global_config


# ============================================================
# config helper
# ============================================================
def _cfg(key: str, default):
    try:
        return global_config.get(key, default)
    except Exception:
        return default


# ============================================================
# core
# ============================================================
def calculate_exit_levels(
    *,
    side: str,
    entry_price: float,
    atr: float,
    confidence: float,
) -> dict:
    """
    Risk Parity に基づく EXIT レベル計算

    Returns:
        {
            "stop_price": float,
            "take_profit_price": float,
        }
    """

    try:
        entry_price = float(entry_price)
        atr = float(atr)
        confidence = float(confidence)
    except Exception:
        return {}

    if entry_price <= 0 or atr <= 0:
        return {}

    STOP_ATR_MULT = _cfg("STOP_ATR_MULTIPLIER", 1.5)
    TAKE_ATR_MULT = _cfg("TAKE_ATR_MULTIPLIER", 2.0)

    # confidence は 0〜1 を想定
    conf_scale = max(0.3, min(1.3, 0.7 + confidence))

    R = atr * STOP_ATR_MULT

    if side == "BUY":
        stop_price = entry_price - R
        take_profit = entry_price + R * TAKE_ATR_MULT * conf_scale
    else:
        stop_price = entry_price + R
        take_profit = entry_price - R * TAKE_ATR_MULT * conf_scale

    return {
        "stop_price": round(stop_price, 2),
        "take_profit_price": round(take_profit, 2),
    }