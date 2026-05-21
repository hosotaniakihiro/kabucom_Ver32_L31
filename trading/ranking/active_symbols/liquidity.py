# ============================================================
# File   : trading/ranking/active_symbols/liquidity.py
# Version: Ver1.3-ACTIVE-SYMBOLS-PREMARKET-PRICE-GUARD
# ------------------------------------------------------------
# Purpose:
#   - PUSH登録候補の流動性/価格フィルタ
#   - 低位株や極端に流動性が低い銘柄を除外する
#   - 監視銘柄を価格条件内に制限する
#
# Ver1.3:
#   - premarket_mode でも最終価格ガードを完全スキップしない。
#   - 価格情報が取れる銘柄は MIN_PRICE / MAX_PRICE を必ず適用する。
#   - 価格情報が本当に無い銘柄だけ fail-open で残す。
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Set

from .config import (
    ENABLE_LIQUIDITY_FILTER,
    KEEP_PROTECTED_EVEN_IF_ILLIQUID,
    MAX_PRICE,
    MIN_PRICE,
    MIN_TICK_COUNT,
    MIN_TRADING_VALUE,
    MIN_VOLUME,
)
from .normalize import dedupe_keep_order, normalize_symbol, to_float
from .ranking_source import build_liquidity_map

logger = logging.getLogger(__name__)


def _has_positive_value(info: Optional[Dict[str, Any]], keys: Iterable[str]) -> bool:
    if not info:
        return False
    for k in keys:
        try:
            if to_float(info.get(k), 0.0) > 0:
                return True
        except Exception:
            pass
    return False


def _has_usable_liquidity_info(info: Optional[Dict[str, Any]]) -> bool:
    if not info:
        return False
    return _has_positive_value(
        info,
        (
            "current_price",
            "price",
            "close",
            "last_price",
            "close_price",
            "現在値",
            "trading_value",
            "turnover",
            "trading_volume",
            "volume",
            "tick_count",
        ),
    )


def _get_price(info: Dict[str, Any]) -> float:
    for k in ("current_price", "price", "close", "last_price", "close_price", "現在値"):
        v = to_float(info.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _get_volume(info: Dict[str, Any]) -> float:
    for k in ("trading_volume", "volume"):
        v = to_float(info.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _get_value(info: Dict[str, Any]) -> float:
    for k in ("trading_value", "turnover"):
        v = to_float(info.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _get_tick(info: Dict[str, Any]) -> float:
    return to_float(info.get("tick_count"), 0.0)


def _price_ok(price: float) -> bool:
    if price <= 0:
        return True
    if MIN_PRICE > 0 and price < MIN_PRICE:
        return False
    if MAX_PRICE > 0 and price > MAX_PRICE:
        return False
    return True


def is_liquid_symbol(
    symbol: Any,
    *,
    liquidity_map: Optional[Dict[str, Dict[str, float]]] = None,
    protected: Optional[Set[str]] = None,
    require_info: bool = False,
) -> bool:
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

    if not _has_usable_liquidity_info(info):
        return not require_info

    assert info is not None

    price = _get_price(info)
    volume = _get_volume(info)
    value = _get_value(info)
    tick = _get_tick(info)

    if not _price_ok(price):
        return False
    if value > 0 and value < MIN_TRADING_VALUE:
        return False
    if volume > 0 and volume < MIN_VOLUME:
        return False
    if tick > 0 and tick < MIN_TICK_COUNT:
        return False

    return True


def filter_liquid_symbols(
    symbols: Iterable[Any],
    *,
    protected: Optional[Set[str]] = None,
    liquidity_map: Optional[Dict[str, Dict[str, float]]] = None,
    context: str = "",
    require_info: bool = False,
) -> List[str]:
    cleaned = dedupe_keep_order(symbols)
    if not ENABLE_LIQUIDITY_FILTER:
        return cleaned

    protected = protected or set()
    liquidity_map = liquidity_map if liquidity_map is not None else build_liquidity_map()

    kept: List[str] = []
    removed: List[str] = []
    missing_info: List[str] = []

    for sym in cleaned:
        info = liquidity_map.get(sym)
        if not _has_usable_liquidity_info(info):
            missing_info.append(sym)

        if is_liquid_symbol(
            sym,
            liquidity_map=liquidity_map,
            protected=protected,
            require_info=require_info,
        ):
            kept.append(sym)
        else:
            removed.append(sym)

    logger.info(
        "[ACTIVE LIQUIDITY FILTER] context=%s before=%d after=%d removed=%d missing_info=%d require_info=%s "
        "min_value=%.0f min_volume=%.0f min_tick=%.0f min_price=%.0f max_price=%.0f removed_head=%s missing_head=%s",
        context,
        len(cleaned),
        len(kept),
        len(removed),
        len(missing_info),
        require_info,
        MIN_TRADING_VALUE,
        MIN_VOLUME,
        MIN_TICK_COUNT,
        MIN_PRICE,
        MAX_PRICE,
        removed[:20],
        missing_info[:20],
    )
    return kept


def final_guard_min_price(
    symbols: Iterable[str],
    *,
    protected: Set[str],
    liquidity_map: Dict[str, Dict[str, float]],
    premarket_mode: bool,
) -> List[str]:
    items = dedupe_keep_order(symbols)

    if not ENABLE_LIQUIDITY_FILTER:
        return items

    protected = protected or set()
    liquidity_map = liquidity_map or {}

    kept: List[str] = []
    removed: List[str] = []
    missing_info: List[str] = []
    price_guarded: List[str] = []

    for s in items:
        sym = normalize_symbol(s)
        if not sym:
            continue

        if sym in protected:
            kept.append(sym)
            continue

        info = liquidity_map.get(sym)

        if not _has_usable_liquidity_info(info):
            kept.append(sym)
            missing_info.append(sym)
            continue

        assert info is not None
        price = _get_price(info)
        if price > 0:
            price_guarded.append(sym)
            if not _price_ok(price):
                removed.append(sym)
                continue

        if is_liquid_symbol(
            sym,
            liquidity_map=liquidity_map,
            protected=protected,
            require_info=False,
        ):
            kept.append(sym)
        else:
            removed.append(sym)

    if removed or missing_info or premarket_mode:
        logger.warning(
            "[ACTIVE FINAL PRICE GUARD] before=%d after=%d removed=%d missing_info_kept=%d "
            "premarket=%s price_guarded=%d min_price=%.1f max_price=%.1f removed_head=%s missing_info_head=%s",
            len(items),
            len(kept),
            len(removed),
            len(missing_info),
            premarket_mode,
            len(price_guarded),
            MIN_PRICE,
            MAX_PRICE,
            removed[:30],
            missing_info[:30],
        )
    else:
        logger.info(
            "[ACTIVE FINAL PRICE GUARD] before=%d after=%d removed=0 missing_info_kept=0 premarket=%s price_guarded=%d min_price=%.1f max_price=%.1f",
            len(items),
            len(kept),
            premarket_mode,
            len(price_guarded),
            MIN_PRICE,
            MAX_PRICE,
        )

    return dedupe_keep_order(kept)
