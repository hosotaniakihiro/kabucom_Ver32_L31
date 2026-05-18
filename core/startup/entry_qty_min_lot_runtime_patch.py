# ============================================================
# File   : core/startup/entry_qty_min_lot_runtime_patch.py
# Version: Ver01-ENTRY-QTY-MIN-LOT-FALLBACK
# ------------------------------------------------------------
# entry_controller の数量計算が0株を返した場合でも、
# 価格帯OK・70万円以内・100株購入可能なら最低100株に戻す。
# 計算値/DB値は変えず、発注直前の最終防衛だけを追加する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIGINAL = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def _get_budget() -> float:
    try:
        from trading.entry.entry_budget import get_max_entry_oneshot_yen
        v = float(get_max_entry_oneshot_yen())
        if v > 0:
            return v
    except Exception:
        pass
    return _safe_float(os.getenv("MAX_ENTRY_ONESHOT_YEN"), 700000.0)


def _get_lot() -> int:
    try:
        from trading.entry.entry_budget import get_order_lot_size
        v = int(get_order_lot_size())
        if v > 0:
            return v
    except Exception:
        pass
    return _safe_int(os.getenv("ORDER_LOT_SIZE"), 100)


def _price_range_ok(price: float) -> bool:
    try:
        from trading.entry.entry_budget import can_afford_min_lot
        ok, diag = can_afford_min_lot(price)
        if not ok:
            logger.warning("[ENTRY QTY MINLOT PATCH] affordability NG price=%s diag=%s", price, diag)
        return bool(ok)
    except Exception:
        min_price = _safe_float(os.getenv("ENTRY_MIN_PRICE"), 3000.0)
        max_price = _safe_float(os.getenv("ENTRY_MAX_PRICE"), 7000.0)
        return bool(price >= min_price and price <= max_price)


def _patched_calculate_entry_quantity(*, symbol: str, price: float, confidence: float, lot_multiplier: float, atr: Any = None) -> int:
    global _ORIGINAL
    qty = 0
    try:
        if callable(_ORIGINAL):
            qty = int(_ORIGINAL(symbol=symbol, price=price, confidence=confidence, lot_multiplier=lot_multiplier, atr=atr))
    except Exception:
        logger.exception("[ENTRY QTY MINLOT PATCH] original lot_sizer failed symbol=%s", symbol)
        qty = 0

    if qty > 0:
        return qty

    if not _env_bool("ENTRY_MIN_LOT_FALLBACK_WHEN_AFFORDABLE", True):
        return 0

    p = _safe_float(price, 0.0)
    if p <= 0:
        return 0

    budget = _get_budget()
    lot = _get_lot()
    if lot <= 0:
        lot = 100

    if not _price_range_ok(p):
        return 0

    max_qty = int((budget // p) // lot * lot)
    if max_qty < lot:
        logger.warning(
            "[ENTRY QTY MINLOT PATCH] cannot afford min lot symbol=%s price=%.1f budget=%.0f lot=%s max_qty=%s",
            symbol,
            p,
            budget,
            lot,
            max_qty,
        )
        return 0

    logger.warning(
        "[ENTRY QTY MINLOT PATCH] qty 0 -> min lot fallback symbol=%s price=%.1f qty=%s budget=%.0f lot=%s confidence=%s multiplier=%s atr=%s",
        symbol,
        p,
        lot,
        budget,
        lot,
        confidence,
        lot_multiplier,
        atr,
    )
    return int(lot)


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        old = getattr(ec, "calculate_entry_quantity", None)
        if callable(old) and getattr(old, "_entry_qty_minlot_patch_v1", False):
            _INSTALLED = True
            return True
        _ORIGINAL = old
        _patched_calculate_entry_quantity._entry_qty_minlot_patch_v1 = True  # type: ignore[attr-defined]
        ec.calculate_entry_quantity = _patched_calculate_entry_quantity
        _INSTALLED = True
        logger.warning("[ENTRY QTY MINLOT PATCH] installed")
        return True
    except Exception:
        logger.exception("[ENTRY QTY MINLOT PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY QTY MINLOT PATCH] auto install failed")

__all__ = ["install"]
