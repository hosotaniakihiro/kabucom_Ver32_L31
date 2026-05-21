# ============================================================
# File   : trading/exit/blowoff_profit_take.py
# Version: V1.0-BLOWOFF-PROFIT-TAKE
# ------------------------------------------------------------
# 株価が吹いた瞬間に通常EXIT判定より前で利確する。
# - 100株など小ロット: 利益到達で全利確
# - 200株以上: 軽い吹き上げで一部利確、大きい吹き上げで全利確
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict

from trading.exit.exit_price_source import get_latest_exit_price
from trading.exit.exit_finalize import finalize_exit
from trading.exit.partial_profit_executor import execute_partial_profit

logger = logging.getLogger(__name__)

BLOWOFF_PROFIT_TAKE_ENABLED = str(os.getenv("BLOWOFF_PROFIT_TAKE_ENABLED", "1")).lower() not in {"0", "false", "no", "off"}
BLOWOFF_SMALL_QTY_FULL_TAKE_PCT = float(os.getenv("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT", "0.20"))
BLOWOFF_PARTIAL_TAKE_PCT = float(os.getenv("BLOWOFF_PARTIAL_TAKE_PCT", "0.25"))
BLOWOFF_FULL_TAKE_PCT = float(os.getenv("BLOWOFF_FULL_TAKE_PCT", "0.45"))
BLOWOFF_SMALL_QTY_MAX = int(float(os.getenv("BLOWOFF_SMALL_QTY_MAX", "199")))
BLOWOFF_PARTIAL_RATIO = float(os.getenv("BLOWOFF_PARTIAL_RATIO", "0.50"))


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _get(pos: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(pos, dict):
        return default
    for k in keys:
        v = pos.get(k)
        if v not in (None, ""):
            return v
    return default


def _normalize_side(v: Any) -> str:
    s = str(v or "").upper().strip()
    if s in {"BUY", "BUY_CREDIT", "LONG", "2", "信用買", "買", "買建"}:
        return "BUY"
    if s in {"SELL", "SELL_CREDIT", "SHORT", "1", "信用売", "売", "売建"}:
        return "SELL"
    return s


def _entry_price(pos: Dict[str, Any]) -> float:
    for k in ("avg_price", "entry_price", "AveragePrice", "average_price", "AvgPrice", "ExecutionPrice", "execution_price", "filled_price", "contract_price", "hold_price"):
        x = _safe_float(_get(pos, k), 0.0)
        if x > 0:
            return x
    src = str(_get(pos, "_position_source", default="") or "").upper()
    if "DB" in src:
        x = _safe_float(_get(pos, "Price", "price"), 0.0)
        if x > 0:
            return x
    return 0.0


def _profit_pct(side: str, entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    if side == "BUY":
        return (price - entry) / entry * 100.0
    if side == "SELL":
        return (entry - price) / entry * 100.0
    return 0.0


def _pnl(side: str, entry: float, price: float) -> float:
    if side == "BUY":
        return price - entry
    if side == "SELL":
        return entry - price
    return 0.0


def apply_blowoff_profit_take(*, symbol: str, pos: Dict[str, Any], regime: int = 0) -> bool:
    if not BLOWOFF_PROFIT_TAKE_ENABLED:
        return False
    try:
        side = _normalize_side(_get(pos, "side", "Side", "trade_side", "position_side", "order_side"))
        qty = _safe_int(_get(pos, "qty", "quantity"), 0)
        entry = _entry_price(pos)
        price, _bar = get_latest_exit_price(symbol)
        price = _safe_float(price, 0.0)
        profit = _profit_pct(side, entry, price)

        if side not in {"BUY", "SELL"} or qty <= 0 or entry <= 0 or price <= 0:
            return False

        if qty <= BLOWOFF_SMALL_QTY_MAX and profit >= BLOWOFF_SMALL_QTY_FULL_TAKE_PCT:
            reason = f"BLOWOFF_SMALL_QTY_FULL_TAKE profit={profit:.3f}%>=trigger={BLOWOFF_SMALL_QTY_FULL_TAKE_PCT:.3f}% qty={qty}"
            logger.warning("[BLOWOFF PROFIT TAKE] FULL small symbol=%s side=%s qty=%s entry=%.4f price=%.4f profit=%.3f%%", symbol, side, qty, entry, price, profit)
            finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=0, regime=regime, inago_state=0, pnl=_pnl(side, entry, price), collapse_prob=0.0, ctx=None)
            return True

        if profit >= BLOWOFF_FULL_TAKE_PCT:
            reason = f"BLOWOFF_FULL_TAKE profit={profit:.3f}%>=trigger={BLOWOFF_FULL_TAKE_PCT:.3f}% qty={qty}"
            logger.warning("[BLOWOFF PROFIT TAKE] FULL symbol=%s side=%s qty=%s entry=%.4f price=%.4f profit=%.3f%%", symbol, side, qty, entry, price, profit)
            finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=0, regime=regime, inago_state=0, pnl=_pnl(side, entry, price), collapse_prob=0.0, ctx=None)
            return True

        if qty > BLOWOFF_SMALL_QTY_MAX and profit >= BLOWOFF_PARTIAL_TAKE_PCT:
            reason = f"BLOWOFF_PARTIAL_TAKE profit={profit:.3f}%>=trigger={BLOWOFF_PARTIAL_TAKE_PCT:.3f}% qty={qty}"
            logger.warning("[BLOWOFF PROFIT TAKE] PARTIAL symbol=%s side=%s qty=%s entry=%.4f price=%.4f profit=%.3f%%", symbol, side, qty, entry, price, profit)
            return bool(execute_partial_profit(symbol=symbol, pos=pos, reason=reason, exit_price=price, ratio=BLOWOFF_PARTIAL_RATIO))

        return False
    except Exception:
        logger.exception("[BLOWOFF PROFIT TAKE] failed symbol=%s", symbol)
        return False


__all__ = ["apply_blowoff_profit_take"]
