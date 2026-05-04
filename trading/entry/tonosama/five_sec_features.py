# ============================================================
# File   : trading/entry/tonosama/five_sec_features.py
# Version: Ver1.0-TONOSAMA-ENTRY-FIVE-SEC-FEATURES
# ============================================================
from __future__ import annotations
import logging
from typing import Any
from .config import MIN_5SEC_PRICE_CHANGE_PCT, MIN_5SEC_VOLUME_SURGE_RATIO, MAX_5SEC_DROP_PCT
from .utils import normalize_symbol, safe_float, dict_get_any
logger = logging.getLogger(__name__)

def get_gc_monitor():
    try:
        from core.global_context.context import global_context as GC
        return getattr(GC, "monitor", None)
    except Exception:
        return None

def _call_bar_method(symbol: str, names: list[str]) -> dict[str, Any]:
    monitor = get_gc_monitor()
    if monitor is None:
        return {}
    for name in names:
        fn = getattr(monitor, name, None)
        if not callable(fn):
            continue
        try:
            ret = fn(symbol)
            if isinstance(ret, dict) and ret:
                return ret
        except TypeError:
            try:
                ret = fn()
                if isinstance(ret, dict):
                    item = ret.get(symbol) or ret.get(str(symbol))
                    if isinstance(item, dict) and item:
                        return item
            except Exception:
                pass
        except Exception:
            logger.debug("[TONOSAMA 5SEC] failed method=%s symbol=%s", name, symbol, exc_info=True)
    return {}

def get_latest_5sec_bar(symbol: str) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    return _call_bar_method(symbol, ["get_five_sec_bar", "get_5sec_bar", "get_latest_5sec_bar", "get_latest_five_sec_bar", "get_bar_5s", "get_5s_bar"]) if symbol else {}

def get_prev_5sec_bar(symbol: str) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    return _call_bar_method(symbol, ["get_previous_five_sec_bar", "get_prev_five_sec_bar", "get_previous_5sec_bar", "get_prev_5sec_bar", "get_prev_bar_5s"]) if symbol else {}

def _bar_price(bar: dict[str, Any]) -> float:
    return safe_float(dict_get_any(bar, "close", "Close", "price", "current_price", "CurrentPrice", "last_price", "last", "close_price", default=0.0), 0.0)
def _bar_open(bar: dict[str, Any]) -> float:
    return safe_float(dict_get_any(bar, "open", "Open", "open_price", "first_price", default=0.0), 0.0)
def _bar_high(bar: dict[str, Any]) -> float:
    return safe_float(dict_get_any(bar, "high", "High", "high_price", default=0.0), 0.0)
def _bar_low(bar: dict[str, Any]) -> float:
    return safe_float(dict_get_any(bar, "low", "Low", "low_price", default=0.0), 0.0)
def _bar_volume(bar: dict[str, Any]) -> float:
    return safe_float(dict_get_any(bar, "volume", "Volume", "trading_volume", "qty", "tick_volume", default=0.0), 0.0)

def get_recent_5sec_bars(symbol: str) -> list[dict[str, Any]]:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return []
    monitor = get_gc_monitor()
    if monitor is not None:
        for name in ["get_recent_five_sec_bars", "get_recent_5sec_bars", "get_last_5sec_bars", "get_5s_bars"]:
            fn = getattr(monitor, name, None)
            if not callable(fn):
                continue
            try:
                ret = fn(symbol)
                if isinstance(ret, (list, tuple)):
                    return [x for x in ret if isinstance(x, dict)]
            except TypeError:
                try:
                    ret = fn(symbol, 5)
                    if isinstance(ret, (list, tuple)):
                        return [x for x in ret if isinstance(x, dict)]
                except Exception:
                    pass
            except Exception:
                logger.debug("[TONOSAMA 5SEC] recent bars failed method=%s symbol=%s", name, symbol, exc_info=True)
    bars=[]
    prev=get_prev_5sec_bar(symbol)
    latest=get_latest_5sec_bar(symbol)
    if prev: bars.append(prev)
    if latest: bars.append(latest)
    return bars

def build_5sec_features(symbol: str) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    bars = get_recent_5sec_bars(symbol)
    latest = bars[-1] if bars else {}
    prev = bars[-2] if len(bars) >= 2 else {}
    latest_close = _bar_price(latest)
    latest_open = _bar_open(latest)
    latest_volume = _bar_volume(latest)
    prev_close = _bar_price(prev)
    prev_volume = _bar_volume(prev)
    base_price = latest_open if latest_open > 0 else prev_close
    price_change_pct = (latest_close - base_price) / base_price * 100.0 if latest_close > 0 and base_price > 0 else 0.0
    vols = [_bar_volume(b) for b in bars[:-1]]
    vols = [v for v in vols if v > 0]
    if latest_volume > 0 and vols:
        avg_prev_volume = sum(vols) / len(vols)
        volume_surge_ratio = latest_volume / avg_prev_volume if avg_prev_volume > 0 else 0.0
    elif latest_volume > 0 and prev_volume > 0:
        avg_prev_volume = prev_volume
        volume_surge_ratio = latest_volume / prev_volume
    else:
        avg_prev_volume = 0.0
        volume_surge_ratio = 0.0
    is_up = price_change_pct >= MIN_5SEC_PRICE_CHANGE_PCT
    is_drop = price_change_pct <= MAX_5SEC_DROP_PCT
    volume_ok = True
    if latest_volume > 0 and avg_prev_volume > 0:
        volume_ok = volume_surge_ratio >= MIN_5SEC_VOLUME_SURGE_RATIO
    confirm_ok = bool(is_up and volume_ok and not is_drop)
    return {"has_5sec_bar": bool(latest), "latest_5sec_close": latest_close, "latest_5sec_open": latest_open, "latest_5sec_high": _bar_high(latest), "latest_5sec_low": _bar_low(latest), "latest_5sec_volume": latest_volume, "prev_5sec_close": prev_close, "prev_5sec_volume": prev_volume, "avg_prev_5sec_volume": avg_prev_volume, "price_change_5s_pct": price_change_pct, "volume_surge_ratio_5s": volume_surge_ratio, "is_5sec_up": bool(is_up), "is_5sec_drop": bool(is_drop), "is_5sec_confirm_ok": bool(confirm_ok)}
