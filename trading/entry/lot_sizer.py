# ============================================================
# File   : trading/entry/lot_sizer.py
# Ver1.2-FINAL-RISK-PARITY-LOT-SIZER-ONESHOT-CAP
# ------------------------------------------------------------
# ✔ confidence / lot_multiplier / ATR から数量を算出
# ✔ 1トレードの最大損失額を常に一定化
# ✔ ドローダウン連動で MAX_RISK_YEN を自動縮小
# ✔ ENTRY_GATE の外側で使用（副作用ゼロ）
# ✔ 約定単位・最小ロット・最大ロット対応
# ✔ 50万円ワンショット制限に合わせて数量を自動縮小
# ✔ None / NaN / 異常値 完全防御
# ============================================================

from __future__ import annotations

import logging

from config import global_config
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


def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
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
    リスク一定化に基づく ENTRY 数量計算。

    重要:
      下位の kabu_api.buy_sell_entry には 50万円ワンショット制限がある。
      ここで price * qty <= MAX_ENTRY_ONESHOT_YEN に収めておくことで、
      ORDER_BUILD_OK 後に ONESHOT制限超過で None になるのを防ぐ。

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
    MAX_RISK_YEN = _safe_float(_cfg("MAX_ENTRY_RISK_YEN", 30_000), 30_000)
    STOP_ATR_MULT = _safe_float(_cfg("STOP_ATR_MULTIPLIER", 1.5), 1.5)
    MIN_STOP_YEN = _safe_float(_cfg("MIN_STOP_YEN", 20), 20)
    MAX_QTY = _safe_int(_cfg("MAX_ENTRY_QTY", 10_000), 10_000)
    LOT_SIZE = _safe_int(_cfg("ORDER_LOT_SIZE", 100), 100)
    MAX_ONESHOT_YEN = _safe_float(_cfg("MAX_ENTRY_ONESHOT_YEN", 500_000), 500_000)

    if LOT_SIZE <= 0:
        LOT_SIZE = 100

    if MAX_QTY <= 0:
        MAX_QTY = 10_000

    if MAX_ONESHOT_YEN <= 0:
        MAX_ONESHOT_YEN = 500_000

    # --------------------------------------------------------
    # ドローダウン連動リスク係数
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
    # quantity by risk
    # --------------------------------------------------------
    raw_qty = risk_budget / per_share_risk
    if raw_qty <= 0:
        return 0

    qty = int(raw_qty // LOT_SIZE * LOT_SIZE)
    qty = max(0, min(qty, MAX_QTY))

    # --------------------------------------------------------
    # 50万円ワンショット制限
    # --------------------------------------------------------
    max_qty_by_oneshot = int((MAX_ONESHOT_YEN // price) // LOT_SIZE * LOT_SIZE)

    if max_qty_by_oneshot <= 0:
        logger.warning(
            "[LOT] qty=0 by oneshot cap symbol=%s price=%.2f max_oneshot=%.0f lot_size=%s",
            symbol,
            price,
            MAX_ONESHOT_YEN,
            LOT_SIZE,
        )
        return 0

    if qty > max_qty_by_oneshot:
        logger.warning(
            "[LOT] qty reduced by oneshot cap symbol=%s price=%.2f qty=%s -> %s max_oneshot=%.0f",
            symbol,
            price,
            qty,
            max_qty_by_oneshot,
            MAX_ONESHOT_YEN,
        )
        qty = max_qty_by_oneshot

    if qty == 0:
        logger.debug(
            "[LOT] qty=0 after rounding (%s, raw=%.2f, risk_scale=%.2f)",
            symbol,
            raw_qty,
            risk_scale,
        )

    return qty
