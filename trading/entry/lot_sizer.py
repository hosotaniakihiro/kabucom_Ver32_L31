# ============================================================
# File   : trading/entry/lot_sizer.py
# Ver1.6-UNIFIED-BUDGET-700K-1500-7000
# ------------------------------------------------------------
# ✔ confidence / lot_multiplier / ATR から数量を算出
# ✔ 1トレードの最大損失額を常に一定化
# ✔ ドローダウン連動で MAX_RISK_YEN を自動縮小
# ✔ 1銘柄あたりの発注上限を 70万円 に統一
# ✔ 資金が増えた場合は1銘柄ロット増ではなく銘柄数増で対応する前提
# ✔ 価格帯OKかつ最低1単元を買える場合、リスク丸めで0株にしない
# ============================================================

from __future__ import annotations

import logging

from config import global_config
from AI.risk.drawdown_guard import get_risk_scale

logger = logging.getLogger(__name__)


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


def _env_bool_cfg(key: str, default: bool = True) -> bool:
    v = _cfg(key, "1" if default else "0")
    try:
        from core.startup.settings_ini_loader import get_setting
        if v is None or str(v).strip() == "":
            v = get_setting(key, "1" if default else "0")
    except Exception:
        pass
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok"}:
        return True
    if s in {"0", "false", "no", "n", "off", "ng"}:
        return False
    return bool(default)


def _get_unified_order_lot_size() -> int:
    try:
        from trading.entry.entry_budget import get_order_lot_size
        v = int(get_order_lot_size())
        if v > 0:
            return v
    except Exception:
        pass
    return _safe_int(_cfg("ORDER_LOT_SIZE", 100), 100)


def _get_oneshot_yen_for_price(price: float) -> float:
    try:
        from trading.entry.entry_budget import get_entry_oneshot_yen_for_price
        v = float(get_entry_oneshot_yen_for_price(price))
        if v > 0:
            return v
    except Exception:
        pass
    return _safe_float(_cfg("MAX_ENTRY_ONESHOT_YEN", 700_000), 700_000)


def _can_afford_min_lot(price: float) -> tuple[bool, dict]:
    try:
        from trading.entry.entry_budget import can_afford_min_lot
        return can_afford_min_lot(price)
    except Exception:
        budget = _get_oneshot_yen_for_price(price)
        lot = _get_unified_order_lot_size()
        return bool(price > 0 and lot > 0 and price * lot <= budget and 1500 <= price <= 7000), {
            "price": price,
            "budget_yen": budget,
            "lot_size": lot,
            "reason": "fallback_unified_700k_affordability_check",
        }


def calculate_entry_quantity(
    *,
    symbol: str,
    price: float,
    confidence: float,
    lot_multiplier: float,
    atr: float | None,
) -> int:
    try:
        price = float(price)
        confidence = float(confidence)
        lot_multiplier = float(lot_multiplier)
        atr = float(atr) if atr is not None else None
    except Exception:
        logger.warning("[LOT] invalid numeric input: %s", symbol)
        return 0

    if price <= 0 or confidence <= 0 or lot_multiplier <= 0:
        logger.warning(
            "[LOT] qty=0 invalid guard symbol=%s price=%.2f confidence=%.4f lot_multiplier=%.4f",
            symbol, price, confidence, lot_multiplier,
        )
        return 0

    MAX_RISK_YEN = _safe_float(_cfg("MAX_ENTRY_RISK_YEN", 30_000), 30_000)
    STOP_ATR_MULT = _safe_float(_cfg("STOP_ATR_MULTIPLIER", 1.5), 1.5)
    MIN_STOP_YEN = _safe_float(_cfg("MIN_STOP_YEN", 20), 20)
    MAX_QTY = _safe_int(_cfg("MAX_ENTRY_QTY", 10_000), 10_000)
    LOT_SIZE = _get_unified_order_lot_size()
    MAX_ONESHOT_YEN = _get_oneshot_yen_for_price(price)
    MIN_LOT_FALLBACK = _env_bool_cfg("ENTRY_MIN_LOT_FALLBACK_WHEN_AFFORDABLE", True)

    if LOT_SIZE <= 0:
        LOT_SIZE = 100
    if MAX_QTY <= 0:
        MAX_QTY = 10_000
    if MAX_ONESHOT_YEN <= 0:
        MAX_ONESHOT_YEN = 700_000

    risk_scale = get_risk_scale()
    if risk_scale <= 0:
        logger.info("[LOT] ENTRY STOP by drawdown symbol=%s risk_scale=%.4f", symbol, risk_scale)
        return 0

    afford_ok, afford_diag = _can_afford_min_lot(price)
    if not afford_ok:
        logger.warning("[LOT] qty=0 affordability NG symbol=%s diag=%s", symbol, afford_diag)
        return 0

    max_qty_by_oneshot = int((MAX_ONESHOT_YEN // price) // LOT_SIZE * LOT_SIZE)
    if max_qty_by_oneshot <= 0:
        logger.warning(
            "[LOT] qty=0 by unified oneshot cap symbol=%s price=%.2f max_oneshot=%.0f lot_size=%s",
            symbol, price, MAX_ONESHOT_YEN, LOT_SIZE,
        )
        return 0

    risk_budget = MAX_RISK_YEN * risk_scale * confidence * lot_multiplier
    if atr and atr > 0:
        per_share_risk = max(atr * STOP_ATR_MULT, MIN_STOP_YEN)
    else:
        per_share_risk = max(price * 0.005, MIN_STOP_YEN)

    raw_qty = risk_budget / per_share_risk if risk_budget > 0 and per_share_risk > 0 else 0.0
    qty = int(raw_qty // LOT_SIZE * LOT_SIZE) if raw_qty > 0 else 0
    qty = max(0, min(qty, MAX_QTY))

    if qty <= 0 and MIN_LOT_FALLBACK:
        qty = min(LOT_SIZE, max_qty_by_oneshot, MAX_QTY)
        logger.warning(
            "[LOT] min lot fallback symbol=%s price=%.2f raw_qty=%.2f qty=%s oneshot_budget=%.0f lot=%s confidence=%.4f multiplier=%.4f atr=%s risk_budget=%.2f per_share_risk=%.2f diag=%s",
            symbol, price, raw_qty, qty, MAX_ONESHOT_YEN, LOT_SIZE, confidence, lot_multiplier, atr,
            risk_budget, per_share_risk, afford_diag,
        )

    if qty > max_qty_by_oneshot:
        logger.warning(
            "[LOT] qty reduced by unified oneshot cap symbol=%s price=%.2f qty=%s -> %s max_oneshot=%.0f",
            symbol, price, qty, max_qty_by_oneshot, MAX_ONESHOT_YEN,
        )
        qty = max_qty_by_oneshot

    if qty <= 0:
        logger.warning(
            "[LOT] qty=0 final symbol=%s price=%.2f raw_qty=%.2f risk_scale=%.4f oneshot_budget=%.0f lot=%s max_qty_by_oneshot=%s fallback=%s",
            symbol, price, raw_qty, risk_scale, MAX_ONESHOT_YEN, LOT_SIZE, max_qty_by_oneshot, MIN_LOT_FALLBACK,
        )
        return 0

    return int(qty)
