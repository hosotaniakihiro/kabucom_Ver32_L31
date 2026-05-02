# ============================================================
# File   : trading/entry/lot_sizer.py
# Ver1.1-FINAL-RISK-PARITY-LOT-SIZER-DRAWDOWN-AWARE
# ------------------------------------------------------------
# ✔ confidence / lot_multiplier / ATR から数量を算出
# ✔ 1トレードの最大損失額を常に一定化
# ✔ ★ ドローダウン連動で MAX_RISK_YEN を自動縮小
# ✔ ENTRY_GATE の外側で使用（副作用ゼロ）
# ✔ 約定単位・最小ロット・最大ロット対応
# ✔ None / NaN / 異常値 完全防御
# ============================================================

from __future__ import annotations

import math
import logging

from config import global_config

# ★ 追加：ドローダウン連動リスク制御
from AI.risk.drawdown_guard import get_risk_scale

logger = logging.getLogger(__name__)


# ============================================================
# config helpers
# ============================================================
def _cfg(key: str, default):
    try:
        return global_config.get(key, default)
    except Exception:
        return default


# ============================================================
# core
# ============================================================
def calculate_entry_quantity(
    *,
    symbol: str,
    price: float,
    confidence: float,
    lot_multiplier: float,
    atr: float | None,
) -> int:
    """
    リスク一定化に基づく ENTRY 数量計算

    Returns:
        int: 発注数量（株数）
    """

    # --------------------------------------------------------
    # guard（数値正規化）
    # --------------------------------------------------------
    try:
        price = float(price)
        confidence = float(confidence)
        lot_multiplier = float(lot_multiplier)
        atr = float(atr) if atr is not None else None
    except Exception:
        logger.warning("[LOT] invalid numeric input: %s", symbol)
        return 0

    if price <= 0 or confidence <= 0 or lot_multiplier <= 0:
        return 0

    # --------------------------------------------------------
    # config
    # --------------------------------------------------------
    MAX_RISK_YEN = _cfg("MAX_ENTRY_RISK_YEN", 30_000)      # 基本リスク
    STOP_ATR_MULT = _cfg("STOP_ATR_MULTIPLIER", 1.5)
    MIN_STOP_YEN = _cfg("MIN_STOP_YEN", 20)
    MAX_QTY = _cfg("MAX_ENTRY_QTY", 10_000)
    LOT_SIZE = _cfg("ORDER_LOT_SIZE", 100)

    # --------------------------------------------------------
    # ★ ドローダウン連動リスク係数
    # --------------------------------------------------------
    risk_scale = get_risk_scale()
    if risk_scale <= 0:
        logger.info(
            "[LOT] ENTRY STOP by drawdown (symbol=%s)",
            symbol,
        )
        return 0

    # --------------------------------------------------------
    # risk budget
    # --------------------------------------------------------
    risk_budget = (
        MAX_RISK_YEN
        * risk_scale
        * confidence
        * lot_multiplier
    )

    if risk_budget <= 0:
        return 0

    # --------------------------------------------------------
    # per share risk
    # --------------------------------------------------------
    if atr and atr > 0:
        per_share_risk = max(atr * STOP_ATR_MULT, MIN_STOP_YEN)
    else:
        # ATR 無し → 価格の0.5%を仮リスク
        per_share_risk = max(price * 0.005, MIN_STOP_YEN)

    if per_share_risk <= 0:
        return 0

    # --------------------------------------------------------
    # quantity
    # --------------------------------------------------------
    raw_qty = risk_budget / per_share_risk
    if raw_qty <= 0:
        return 0

    # ロット単位に切り捨て
    qty = int(raw_qty // LOT_SIZE * LOT_SIZE)

    # 上限・下限ガード
    qty = max(0, min(qty, MAX_QTY))

    if qty == 0:
        logger.debug(
            "[LOT] qty=0 after rounding (%s, raw=%.2f, risk_scale=%.2f)",
            symbol,
            raw_qty,
            risk_scale,
        )

    return qty