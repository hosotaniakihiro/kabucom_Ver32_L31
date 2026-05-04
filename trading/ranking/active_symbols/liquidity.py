# ============================================================
# File   : trading/ranking/active_symbols/liquidity.py
# Version: Ver1.0-ACTIVE-SYMBOLS-LIQUIDITY
# ============================================================
from __future__ import annotations
import logging
from typing import Any, Dict, Iterable, List, Optional, Set
from .config import ENABLE_LIQUIDITY_FILTER, KEEP_PROTECTED_EVEN_IF_ILLIQUID, MIN_PRICE, MIN_TICK_COUNT, MIN_TRADING_VALUE, MIN_VOLUME
from .normalize import dedupe_keep_order, normalize_symbol, to_float
from .ranking_source import build_liquidity_map

logger = logging.getLogger(__name__)


def is_liquid_symbol(symbol: Any, *, liquidity_map: Optional[Dict[str, Dict[str, float]]] = None, protected: Optional[Set[str]] = None, require_info: bool = False) -> bool:
    if not ENABLE_LIQUIDITY_FILTER:
        return True
    sym = normalize_symbol(symbol)
    if not sym:
        return False
    protected = protected or set()
    if KEEP_PROTECTED_EVEN_IF_ILLIQUID and sym in protected:
        return True
    liquidity_map = liquidity_map if liquidity_map is not None else build_liquidity_map()
    info = liquidity_map.get(sym)
    if not info:
        return not require_info
    price = to_float(info.get("current_price"), 0.0)
    volume = to_float(info.get("trading_volume"), 0.0)
    value = to_float(info.get("trading_value"), 0.0)
    tick = to_float(info.get("tick_count"), 0.0)
    if price < MIN_PRICE:
        return False
    if value < MIN_TRADING_VALUE:
        return False
    if volume < MIN_VOLUME:
        return False
    if tick < MIN_TICK_COUNT:
        return False
    return True


def filter_liquid_symbols(symbols: Iterable[Any], *, protected: Optional[Set[str]] = None, liquidity_map: Optional[Dict[str, Dict[str, float]]] = None, context: str = "", require_info: bool = False) -> List[str]:
    cleaned = dedupe_keep_order(symbols)
    if not ENABLE_LIQUIDITY_FILTER:
        return cleaned
    protected = protected or set()
    liquidity_map = liquidity_map if liquidity_map is not None else build_liquidity_map()
    kept, removed = [], []
    for sym in cleaned:
        if is_liquid_symbol(sym, liquidity_map=liquidity_map, protected=protected, require_info=require_info):
            kept.append(sym)
        else:
            removed.append(sym)
    logger.info("[ACTIVE LIQUIDITY FILTER] context=%s before=%d after=%d removed=%d require_info=%s min_value=%.0f min_volume=%.0f min_tick=%.0f min_price=%.0f removed_head=%s", context, len(cleaned), len(kept), len(removed), require_info, MIN_TRADING_VALUE, MIN_VOLUME, MIN_TICK_COUNT, MIN_PRICE, removed[:20])
    return kept


def final_guard_min_price(symbols: Iterable[str], *, protected: Set[str], liquidity_map: Dict[str, Dict[str, float]], premarket_mode: bool) -> List[str]:
    items = dedupe_keep_order(symbols)
    if premarket_mode:
        return items
    kept, removed = [], []
    for s in items:
        if s in protected:
            kept.append(s)
            continue
        if is_liquid_symbol(s, liquidity_map=liquidity_map, protected=protected, require_info=True):
            kept.append(s)
        else:
            removed.append(s)
    if removed:
        logger.info("[ACTIVE FINAL MINPRICE GUARD] before=%d after=%d removed=%d min_price=%.1f removed_head=%s", len(items), len(kept), len(removed), MIN_PRICE, removed[:30])
    return kept
