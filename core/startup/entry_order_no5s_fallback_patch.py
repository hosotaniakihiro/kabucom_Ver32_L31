# ============================================================
# File   : core/startup/entry_order_no5s_fallback_patch.py
# Version: V1-RANKING-TONOSAMA-NO5S-FALLBACK
# ------------------------------------------------------------
# 目的:
#   RANKING / TONOSAMA は 5秒足必須ではない方針なのに、
#   entry_order_builder.build_entry_order の非SUMMARY系が
#   five_sec_breakout() 必須で NO_5S_BREAKOUT を返し、
#   final guard ALL_OK 後に ORDER_BUILD_NG で止まる問題を救済する。
#
# 方針:
#   - 既存 build_entry_order をまず実行。
#   - reason=NO_5S_BREAKOUT かつ source が RANKING/TONOSAMA の場合だけ、
#     close/price/current_price を使った安全な LIMIT 指値へフォールバック。
#   - qty_override を尊重。無い場合は calculate_qty_by_budget を使う。
# ============================================================
from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_WATCHER = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _first(row: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    try:
        if isinstance(row, dict):
            for k in keys:
                v = row.get(k)
                if v not in (None, ""):
                    return v
    except Exception:
        pass
    return default


def _source_ok(source: str) -> bool:
    src = str(source or "").upper()
    allow = {x.strip().upper() for x in os.getenv("ENTRY_ORDER_NO5S_FALLBACK_SOURCES", "RANKING,TONOSAMA").split(",") if x.strip()}
    return src in allow or "ALL" in allow


def _fallback_order(*, symbol: str, side: str, source: str, entry_row: dict, qty_override: int | None = None) -> dict:
    try:
        from utils_common import calculate_qty_by_budget, get_tick_size
    except Exception:
        calculate_qty_by_budget = None
        get_tick_size = None
    row = entry_row or {}
    side_u = str(side or "").upper()
    price = _safe_float(_first(row, ("close_price", "price", "current_price", "close", "vwap"), 0.0), 0.0)
    if price <= 0:
        return {"ok": False, "reason": "NO_5S_BREAKOUT_AND_NO_PRICE", "detail": {"source": source, "side": side_u}}
    try:
        tick = float(get_tick_size(price)) if callable(get_tick_size) else 1.0
        if tick <= 0:
            tick = 1.0
    except Exception:
        tick = 1.0
    if side_u == "BUY":
        order_price = math.ceil(price / tick) * tick
    else:
        order_price = max(tick, math.floor(price / tick) * tick)
    if qty_override is not None:
        qty = int(qty_override)
    elif callable(calculate_qty_by_budget):
        qty = int(calculate_qty_by_budget(order_price) or 0)
    else:
        qty = 100
    if qty <= 0 and side_u == "BUY":
        qty = 100
    if qty <= 0:
        return {"ok": False, "reason": "QTY_ZERO_NO5S_FALLBACK", "detail": {"price": order_price, "source": source, "side": side_u}}
    return {
        "ok": True,
        "reason": "OK_NO5S_FALLBACK",
        "detail": {
            "order_type": "LIMIT",
            "price": float(order_price),
            "base_price": float(price),
            "qty": int(qty),
            "spread_pct": None,
            "max_spread_pct": None,
            "board": False,
            "price_source": "no5s_fallback_close_limit",
            "no5s_fallback": True,
            "source": source,
            "side": side_u,
            "qty_override": qty_override is not None,
        },
    }


def _patch_once() -> bool:
    global _INSTALLED
    if not _env_bool("ENTRY_ORDER_NO5S_FALLBACK_ENABLED", True):
        return False
    try:
        import trading.handlers.entry_order_builder as eob
        cur = getattr(eob, "build_entry_order", None)
        if not callable(cur):
            return False
        if getattr(cur, "_entry_order_no5s_fallback_v1", False):
            _INSTALLED = True
            return True
        orig = cur

        def patched_build_entry_order(*args: Any, **kwargs: Any) -> dict:
            ret = orig(*args, **kwargs)
            try:
                if not isinstance(ret, dict) or ret.get("ok", True):
                    return ret
                reason = str(ret.get("reason") or "")
                if reason != "NO_5S_BREAKOUT":
                    return ret
                source = str(kwargs.get("source") or "").upper()
                if not _source_ok(source):
                    return ret
                fb = _fallback_order(
                    symbol=str(kwargs.get("symbol") or ""),
                    side=str(kwargs.get("side") or ""),
                    source=source,
                    entry_row=kwargs.get("entry_row") or {},
                    qty_override=kwargs.get("qty_override"),
                )
                logger.warning("[ENTRY ORDER NO5S FALLBACK] source=%s symbol=%s side=%s original=%s fallback=%s", source, kwargs.get("symbol"), kwargs.get("side"), ret, fb)
                return fb
            except Exception:
                logger.exception("[ENTRY ORDER NO5S FALLBACK] wrapper failed")
                return ret

        patched_build_entry_order._entry_order_no5s_fallback_v1 = True  # type: ignore[attr-defined]
        patched_build_entry_order._original = orig  # type: ignore[attr-defined]
        eob.build_entry_order = patched_build_entry_order
        _INSTALLED = True
        logger.warning("[ENTRY ORDER NO5S FALLBACK] patched sources=%s", os.getenv("ENTRY_ORDER_NO5S_FALLBACK_SOURCES", "RANKING,TONOSAMA"))
        return True
    except Exception:
        logger.exception("[ENTRY ORDER NO5S FALLBACK] patch failed")
        return False


def _watch() -> None:
    for i in range(240):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning("[ENTRY ORDER NO5S FALLBACK] enforce i=%s ok=%s", i, ok)
        time.sleep(0.5)


def install() -> bool:
    global _WATCHER
    os.environ.setdefault("ENTRY_ORDER_NO5S_FALLBACK_ENABLED", "1")
    os.environ.setdefault("ENTRY_ORDER_NO5S_FALLBACK_SOURCES", "RANKING,TONOSAMA")
    ok = _patch_once()
    if not _WATCHER:
        _WATCHER = True
        threading.Thread(target=_watch, name="entry-order-no5s-fallback", daemon=True).start()
    logger.warning("[ENTRY ORDER NO5S FALLBACK] installed ok=%s watcher=%s", ok, _WATCHER)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[ENTRY ORDER NO5S FALLBACK] auto install failed")

__all__ = ["install"]
