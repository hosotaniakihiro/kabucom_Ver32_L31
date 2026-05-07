# ============================================================
# File   : trading/ranking/active_symbols/liquidity.py
# Version: Ver1.1-ACTIVE-SYMBOLS-LIQUIDITY-NO-DROP-MISSING-INFO
# ------------------------------------------------------------
# Purpose:
#   - PUSH登録候補の流動性/価格フィルタ
#   - 低位株や極端に流動性が低い銘柄を除外する
#
# REV1.1:
#   - final_guard_min_price() で liquidity_map に情報が無い銘柄を全削除しない
#   - 寄前気配CSV/ランキング候補など、価格情報が未取得の段階では候補を残す
#   - current_price が 0/None/欠損の場合も「情報不足」として残す
#   - 価格情報がある銘柄だけ MIN_PRICE / volume / value / tick を判定する
#   - before=95 after=0 のようなPUSH対象全消滅を防ぐ
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Set

from .config import (
    ENABLE_LIQUIDITY_FILTER,
    KEEP_PROTECTED_EVEN_IF_ILLIQUID,
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
    """
    フィルタ判定に使える価格/流動性情報があるか。

    ranking DB の列名揺れを考慮する。
    情報が無い場合は「落とす」のではなく「判定保留」にする。
    """
    if not info:
        return False

    return _has_positive_value(
        info,
        (
            "current_price",
            "price",
            "close",
            "last_price",
            "trading_value",
            "turnover",
            "trading_volume",
            "volume",
            "tick_count",
        ),
    )


def _get_price(info: Dict[str, Any]) -> float:
    for k in ("current_price", "price", "close", "last_price"):
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

    if price > 0 and price < MIN_PRICE:
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
        "min_value=%.0f min_volume=%.0f min_tick=%.0f min_price=%.0f removed_head=%s missing_head=%s",
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
    """
    PUSH登録直前の最終ガード。

    重要:
      以前は require_info=True で is_liquid_symbol() を呼んでいたため、
      liquidity_map に価格情報が無い銘柄が全て削除され、
      before=95 after=0 のようにPUSH対象が消えることがあった。

    方針:
      - premarket_mode=True はそのまま返す
      - 保有銘柄/protected は必ず残す
      - 情報が無い銘柄は「判定保留」として残す
      - 情報がある銘柄だけ価格/流動性で落とす
    """
    items = dedupe_keep_order(symbols)

    if premarket_mode:
        logger.info(
            "[ACTIVE FINAL MINPRICE GUARD] skipped premarket_mode before=%d after=%d",
            len(items),
            len(items),
        )
        return items

    if not ENABLE_LIQUIDITY_FILTER:
        return items

    protected = protected or set()
    liquidity_map = liquidity_map or {}

    kept: List[str] = []
    removed: List[str] = []
    missing_info: List[str] = []

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

        if is_liquid_symbol(
            sym,
            liquidity_map=liquidity_map,
            protected=protected,
            require_info=False,
        ):
            kept.append(sym)
        else:
            removed.append(sym)

    if removed or missing_info:
        logger.warning(
            "[ACTIVE FINAL MINPRICE GUARD] before=%d after=%d removed=%d missing_info_kept=%d "
            "min_price=%.1f removed_head=%s missing_info_head=%s",
            len(items),
            len(kept),
            len(removed),
            len(missing_info),
            MIN_PRICE,
            removed[:30],
            missing_info[:30],
        )
    else:
        logger.info(
            "[ACTIVE FINAL MINPRICE GUARD] before=%d after=%d removed=0 missing_info_kept=0 min_price=%.1f",
            len(items),
            len(kept),
            MIN_PRICE,
        )

    return dedupe_keep_order(kept)
