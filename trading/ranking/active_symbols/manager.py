# ============================================================
# File   : trading/ranking/active_symbols/manager.py
# Version: Ver1.0-ACTIVE-SYMBOLS-MANAGER
# ============================================================
from __future__ import annotations
import datetime as dt, logging
from typing import Iterable, List, Set, Tuple
from global_state import global_data
from .config import ACTIVE_REQUIRE_SYMBOL_FLAGS, ENABLE_PREMARKET_SBI, ENABLE_LIQUIDITY_FILTER, MAX_ACTIVE_SYMBOLS, MIN_PRICE, MIN_TICK_COUNT, MIN_TRADING_VALUE, MIN_VOLUME, TARGET_ACTIVE_SYMBOLS, USE_PREMARKET_WHEN_TODAY_RANKING_EMPTY
from .global_helpers import get_global_attr, set_global_attr
from .liquidity import final_guard_min_price, filter_liquid_symbols, is_liquid_symbol
from .normalize import dedupe_keep_order, normalize_symbol, now as now_dt, to_float
from .premarket_source import filter_premarket_min_price, is_premarket_time, load_premarket_symbols
from .protected import get_protected_symbols
from .ranking_source import build_liquidity_map, extract_volume_speed_symbols, today_ranking_available, today_ranking_symbols, update_last_seen_from_ranking
from .reflect import reflect_active_to_global
from .symbol_flags import filter_by_symbol_flags, load_symbol_flags_eligible_symbols

logger = logging.getLogger(__name__)


def _build_today_ranking_candidates(*, now: dt.datetime, eligible_symbols: Set[str], protected: Set[str], liquidity_map: dict[str, dict[str, float]]) -> Tuple[List[str], Set[str], str]:
    today_symbols = today_ranking_symbols(now=now)
    universe = set(today_symbols)
    logger.info("[ACTIVE SOURCE] today_ranking symbols=%d head=%s", len(today_symbols), today_symbols[:20])
    candidates = filter_by_symbol_flags(today_symbols, eligible_symbols=eligible_symbols, context="today_ranking")
    candidates = filter_liquid_symbols(candidates, protected=protected, liquidity_map=liquidity_map, context="today_ranking", require_info=True)
    return candidates, universe, "today_ranking"


def _build_premarket_candidates(*, now: dt.datetime, eligible_symbols: Set[str], protected: Set[str]) -> Tuple[List[str], Set[str], str]:
    premarket_symbols = load_premarket_symbols(now=now)
    universe = set(premarket_symbols)
    logger.info("[ACTIVE SOURCE] premarket_sbi symbols=%d head=%s", len(premarket_symbols), premarket_symbols[:20])
    candidates = filter_by_symbol_flags(premarket_symbols, eligible_symbols=eligible_symbols, context="premarket_sbi")
    candidates = filter_premarket_min_price(candidates, now=now, protected=protected)
    return candidates, universe, "premarket_sbi"


def _iter_fallback_sources(prev_active: Iterable[str], hot_symbols: Iterable[str], primary_candidates: Iterable[str]) -> Iterable[str]:
    for s in primary_candidates:
        yield str(s)
    for s in hot_symbols:
        yield str(s)
    for s in prev_active:
        yield str(s)
    for attr in ("candidate_push_symbols", "push_candidate_symbols", "push_symbols_100", "monitor_symbols"):
        vals = get_global_attr(attr, [])
        for s in vals or []:
            yield str(s)


def _trim_to_max(symbols: Iterable[str], *, protected: Set[str], liquidity_map: dict[str, dict[str, float]]) -> Set[str]:
    items = dedupe_keep_order(symbols)
    if len(items) <= MAX_ACTIVE_SYMBOLS:
        return set(items)
    def sort_key(sym: str):
        info = liquidity_map.get(sym, {})
        is_protected = 1 if sym in protected else 0
        value = to_float(info.get("trading_value"), 0.0)
        volume = to_float(info.get("trading_volume"), 0.0)
        tick = to_float(info.get("tick_count"), 0.0)
        try:
            last_seen = global_data.symbol_last_seen.get(sym, dt.datetime.min)
        except Exception:
            last_seen = dt.datetime.min
        if last_seen is None:
            last_seen = dt.datetime.min
        return (is_protected, value, volume, tick, last_seen)
    return set(sorted(items, key=sort_key, reverse=True)[:MAX_ACTIVE_SYMBOLS])


def update_active_symbols(force: bool = False) -> List[str]:
    try:
        return _update_active_symbols_impl(force=force)
    except Exception:
        logger.exception("[ACTIVE] update_active_symbols failed")
        try:
            prev = dedupe_keep_order(getattr(global_data, "symbols_active", []))
            if prev:
                return prev
        except Exception:
            pass
        return []


def _update_active_symbols_impl(force: bool = False) -> List[str]:
    del force
    n = now_dt()
    if not hasattr(global_data, "symbol_last_seen"):
        global_data.symbol_last_seen = {}
    if not hasattr(global_data, "symbols_active"):
        global_data.symbols_active = set()
    prev_active: Set[str] = set(dedupe_keep_order(global_data.symbols_active))
    protected = get_protected_symbols()
    update_last_seen_from_ranking(n)
    eligible_symbols, _flag_info = load_symbol_flags_eligible_symbols()
    liquidity_map = build_liquidity_map()
    today_available = today_ranking_available(now=n)
    premarket_mode = ENABLE_PREMARKET_SBI and (is_premarket_time(n) or (USE_PREMARKET_WHEN_TODAY_RANKING_EMPTY and not today_available))
    if premarket_mode:
        primary_candidates, allowed_universe, source_name = _build_premarket_candidates(now=n, eligible_symbols=eligible_symbols, protected=protected)
    else:
        primary_candidates, allowed_universe, source_name = _build_today_ranking_candidates(now=n, eligible_symbols=eligible_symbols, protected=protected, liquidity_map=liquidity_map)
    active: Set[str] = set(primary_candidates)
    active |= protected
    hot_symbols = extract_volume_speed_symbols()
    hot_symbols_allowed = {s for s in dedupe_keep_order(hot_symbols) if s in allowed_universe or s in protected}
    if not premarket_mode:
        hot_symbols_allowed = set(filter_liquid_symbols(hot_symbols_allowed, protected=protected, liquidity_map=liquidity_map, context="hot_symbols", require_info=True))
    active |= hot_symbols_allowed
    skipped_outside_universe: List[str] = []
    skipped_flags_or_liq: List[str] = []
    if len(active) < TARGET_ACTIVE_SYMBOLS:
        for sym in _iter_fallback_sources(prev_active, hot_symbols_allowed, primary_candidates):
            ns = normalize_symbol(sym)
            if not ns or ns in active:
                continue
            if ns not in allowed_universe and ns not in protected:
                skipped_outside_universe.append(ns)
                continue
            if ACTIVE_REQUIRE_SYMBOL_FLAGS and ns not in eligible_symbols and ns not in protected:
                skipped_flags_or_liq.append(ns)
                continue
            if not premarket_mode:
                if not is_liquid_symbol(ns, liquidity_map=liquidity_map, protected=protected, require_info=True):
                    skipped_flags_or_liq.append(ns)
                    continue
            active.add(ns)
            if len(active) >= TARGET_ACTIVE_SYMBOLS:
                break
    active = _trim_to_max(active, protected=protected, liquidity_map=liquidity_map)
    active_list = final_guard_min_price(active, protected=protected, liquidity_map=liquidity_map, premarket_mode=premarket_mode)
    reflect_active_to_global(active_list)
    set_global_attr("active_symbol_source", source_name)
    set_global_attr("active_symbol_premarket_mode", premarket_mode)
    set_global_attr("active_symbol_today_ranking_available", today_available)
    set_global_attr("active_symbol_allowed_universe_size", len(allowed_universe))
    logger.info("[ACTIVE] total=%d source=%s premarket=%s today_ranking_available=%s allowed_universe=%d protected=%d last_seen=%d hot=%d liquidity_filter=%s min_value=%.0f min_volume=%.0f min_tick=%.0f min_price=%.0f skipped_outside_universe=%d skipped_flags_or_liq=%d head=%s", len(active_list), source_name, premarket_mode, today_available, len(allowed_universe), len(protected), len(global_data.symbol_last_seen), len(hot_symbols_allowed), ENABLE_LIQUIDITY_FILTER, MIN_TRADING_VALUE, MIN_VOLUME, MIN_TICK_COUNT, MIN_PRICE, len(skipped_outside_universe), len(skipped_flags_or_liq), active_list[:10])
    if len(active_list) < TARGET_ACTIVE_SYMBOLS:
        logger.warning("[ACTIVE] below target total=%d target=%d source=%s reason=allowed_universe_or_minprice_limited outside_head=%s flags_or_liq_head=%s", len(active_list), TARGET_ACTIVE_SYMBOLS, source_name, skipped_outside_universe[:20], skipped_flags_or_liq[:20])
    return active_list


def get_active_symbols(*args, **kwargs) -> List[str]:
    del args, kwargs
    symbols = dedupe_keep_order(getattr(global_data, "symbols_active", []))
    if not symbols:
        symbols = dedupe_keep_order(get_global_attr("active_symbols", []))
    if not symbols:
        symbols = dedupe_keep_order(get_global_attr("monitor_symbols", []))
    try:
        if not is_premarket_time(now_dt()):
            symbols = final_guard_min_price(symbols, protected=get_protected_symbols(), liquidity_map=build_liquidity_map(), premarket_mode=False)
    except Exception:
        logger.debug("[ACTIVE] getter min price guard failed", exc_info=True)
    return symbols[:MAX_ACTIVE_SYMBOLS]


def get_current_active_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()

def get_monitor_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]

def get_push_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]

def get_register_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]

def get_subscription_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]

def get_rotation_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]


def debug_active_symbols() -> dict:
    symbols = get_active_symbols()
    liquidity_map = build_liquidity_map()
    liquid = [s for s in symbols if is_liquid_symbol(s, liquidity_map=liquidity_map, protected=get_protected_symbols(), require_info=False)]
    payload = {"total": len(symbols), "liquid_total": len(liquid), "head": symbols[:20], "source": get_global_attr("active_symbol_source", None), "premarket_mode": get_global_attr("active_symbol_premarket_mode", None), "today_ranking_available": get_global_attr("active_symbol_today_ranking_available", None), "allowed_universe_size": get_global_attr("active_symbol_allowed_universe_size", None), "liquidity_filter": ENABLE_LIQUIDITY_FILTER, "min_trading_value": MIN_TRADING_VALUE, "min_volume": MIN_VOLUME, "min_tick_count": MIN_TICK_COUNT, "min_price": MIN_PRICE, "last_seen": len(getattr(global_data, "symbol_last_seen", {}) or {})}
    logger.info("[ACTIVE DEBUG] %s", payload)
    return payload
