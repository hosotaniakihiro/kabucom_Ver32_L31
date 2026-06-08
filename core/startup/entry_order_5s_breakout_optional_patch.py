# ============================================================
# File   : core/startup/entry_order_5s_breakout_optional_patch.py
# Version: V1-NO-5S-BREAKOUT-FALLBACK-LIMIT
# ------------------------------------------------------------
# trading.handlers.entry_order_builder.build_entry_order で
# RANKING / TONOSAMA / EARLY_SCALP が 5秒足ブレイク無しだけを理由に
# NO_5S_BREAKOUT で注文生成NGになる問題を緩和する。
#
# ユーザー方針:
#   - 5秒足は必須にしない。
#
# 方針:
#   - 元の build_entry_order を先に実行する。
#   - ret.ok=True なら何もしない。
#   - ret.reason == NO_5S_BREAKOUT の場合だけ、entry_row の close/price から
#     LIMIT 注文を作る。
#   - LOW_LIQUIDITY / QTY_ZERO など他理由は変更しない。
#   - SUMMARY_AI は既存の5秒足ガード/板touch系を尊重し、原則対象外。
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _first(row: Dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        try:
            v = row.get(name)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _row_to_dict(row: Any) -> Dict[str, Any]:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _ok(**kwargs: Any) -> Dict[str, Any]:
    return {"ok": True, "reason": "OK", "detail": kwargs}


def _chain_contains(fn: Any, marker: str, *, limit: int = 30) -> bool:
    seen: set[int] = set()
    cur = fn
    for _ in range(limit):
        if not callable(cur):
            return False
        ident = id(cur)
        if ident in seen:
            return False
        seen.add(ident)
        if getattr(cur, marker, False):
            return True
        cur = (
            getattr(cur, "_entry_5s_breakout_optional_original", None)
            or getattr(cur, "_entry_board_touch_original", None)
            or getattr(cur, "_entry_price_improvement_original", None)
            or getattr(cur, "_original", None)
        )
    return False


def _source_allowed(source: str) -> bool:
    src = str(source or "").upper()
    allowed = {
        s.strip().upper()
        for s in os.getenv("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_SOURCES", "RANKING,TONOSAMA,EARLY_SCALP,ENTRY").split(",")
        if s.strip()
    }
    return "ALL" in allowed or src in allowed


def _fallback_limit_order(*, eob: Any, symbol: str, side: str, source: str, entry_row: Dict[str, Any], qty_override: Optional[int]) -> Dict[str, Any]:
    side_u = str(side or "").upper()
    source_u = str(source or "").upper()
    base_price = _safe_float(
        _first(entry_row, ("close_price", "price", "current_price", "close", "vwap"), 0.0),
        0.0,
    )
    if base_price <= 0:
        return {"ok": False, "reason": "NO_PRICE_SOURCE_AFTER_NO_5S_BREAKOUT", "detail": {"symbol": symbol, "side": side_u, "source": source_u}}

    try:
        round_price = getattr(eob, "_round_price")
        price = float(round_price(base_price, side_u))
    except Exception:
        price = float(base_price)

    volume = _safe_float(_first(entry_row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    trading_value = price * volume if price > 0 else 0.0
    min_turnover = _env_float("ENTRY_ORDER_5S_FALLBACK_MIN_TRADING_VALUE", 3_000_000.0)
    if trading_value < min_turnover:
        return {
            "ok": False,
            "reason": "LOW_LIQUIDITY_AFTER_NO_5S_BREAKOUT",
            "detail": {"symbol": symbol, "side": side_u, "source": source_u, "price": price, "volume": volume, "trading_value": trading_value, "min_trading_value": min_turnover},
        }

    if qty_override is not None:
        qty = int(qty_override)
        if qty <= 0:
            return {"ok": False, "reason": "INVALID_QTY_OVERRIDE", "detail": {"qty_override": qty_override}}
    else:
        try:
            from utils_common import calculate_qty_by_budget
            qty = int(calculate_qty_by_budget(price))
        except Exception:
            qty = 0
        if qty <= 0 and side_u == "BUY":
            qty = 100
    if qty <= 0:
        return {"ok": False, "reason": "QTY_ZERO_AFTER_NO_5S_BREAKOUT", "detail": {"symbol": symbol, "side": side_u, "price": price, "base_price": base_price}}

    detail = {
        "order_type": "LIMIT",
        "price": price,
        "base_price": base_price,
        "qty": qty,
        "spread_pct": None,
        "max_spread_pct": None,
        "board": False,
        "price_source": "close_limit_fallback_no_5s_breakout",
        "fallback_from": "NO_5S_BREAKOUT",
        "five_sec_breakout_optional": True,
        "source": source_u,
        "symbol": symbol,
        "side": side_u,
        "trading_value": trading_value,
    }
    if qty_override is not None:
        detail["qty_override"] = True
    logger.warning(
        "[ENTRY ORDER 5S OPTIONAL] ALLOW_NO_5S_BREAKOUT symbol=%s side=%s source=%s price=%.4f base=%.4f qty=%s volume=%.0f trading_value=%.0f",
        symbol,
        side_u,
        source_u,
        price,
        base_price,
        qty,
        volume,
        trading_value,
    )
    return _ok(**detail)


def install() -> bool:
    global _INSTALLED
    if not _env_bool("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_ENABLED", True):
        logger.warning("[ENTRY ORDER 5S OPTIONAL] disabled by env")
        return False
    try:
        import trading.handlers.entry_order_builder as eob
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY ORDER 5S OPTIONAL] import failed")
        return False

    old_build = getattr(eob, "build_entry_order", None)
    if not callable(old_build):
        logger.warning("[ENTRY ORDER 5S OPTIONAL] build_entry_order not callable")
        return False
    if _chain_contains(old_build, "_entry_5s_breakout_optional_wrapped"):
        _INSTALLED = True
        logger.warning("[ENTRY ORDER 5S OPTIONAL] already present in wrapper chain")
        return True

    def build_entry_order_5s_optional(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        ret = old_build(*args, **kwargs)
        try:
            if not isinstance(ret, dict):
                return ret
            if ret.get("ok"):
                return ret
            reason = str(ret.get("reason") or "")
            if reason != "NO_5S_BREAKOUT":
                return ret

            source = str(kwargs.get("source") or "").upper()
            if not _source_allowed(source):
                return ret
            if source == "SUMMARY_AI" and not _env_bool("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_FOR_SUMMARY", False):
                return ret

            symbol = str(kwargs.get("symbol") or "").strip()
            side = str(kwargs.get("side") or "").upper().strip()
            entry_row = _row_to_dict(kwargs.get("entry_row"))
            qty_override = kwargs.get("qty_override")
            return _fallback_limit_order(eob=eob, symbol=symbol, side=side, source=source, entry_row=entry_row, qty_override=qty_override)
        except Exception:
            logger.exception("[ENTRY ORDER 5S OPTIONAL] fallback failed; returning original ret")
            return ret

    build_entry_order_5s_optional._entry_5s_breakout_optional_wrapped = True  # type: ignore[attr-defined]
    build_entry_order_5s_optional._entry_5s_breakout_optional_original = old_build  # type: ignore[attr-defined]
    build_entry_order_5s_optional._original = old_build  # type: ignore[attr-defined]
    setattr(eob, "build_entry_order", build_entry_order_5s_optional)
    try:
        setattr(ec, "build_entry_order", build_entry_order_5s_optional)
    except Exception:
        logger.debug("[ENTRY ORDER 5S OPTIONAL] entry_controller alias patch skipped", exc_info=True)

    _INSTALLED = True
    logger.warning(
        "[ENTRY ORDER 5S OPTIONAL] installed enabled=True sources=%s min_trading_value=%.0f summary_enabled=%s",
        os.getenv("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_SOURCES", "RANKING,TONOSAMA,EARLY_SCALP,ENTRY"),
        _env_float("ENTRY_ORDER_5S_FALLBACK_MIN_TRADING_VALUE", 3_000_000.0),
        _env_bool("ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_FOR_SUMMARY", False),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY ORDER 5S OPTIONAL] auto install failed")


__all__ = ["install"]
