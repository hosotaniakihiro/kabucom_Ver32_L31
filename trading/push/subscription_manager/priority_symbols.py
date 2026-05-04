# ============================================================
# File   : trading/push/subscription_manager/priority_symbols.py
# Version: V1.0-PUSH-SUBSCRIPTION-PRIORITY-SYMBOLS
# ------------------------------------------------------------
# Purpose:
#   - 保有中銘柄
#   - 発注中銘柄
#   - 直近エントリー銘柄
#   をPUSH購読から落とさないために収集する。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

from .globals_access import safe_get_global_data, safe_getattr, safe_setattr
from .rotation import normalize_symbol, normalize_symbols
from .symbols import dedupe_keep_order

logger = logging.getLogger(__name__)


def _collect_position_symbols_from_obj(obj: Any) -> list[str]:
    symbols: list[str] = []

    try:
        if obj is None:
            return []

        positions = None

        if hasattr(obj, "snapshot_open"):
            positions = obj.snapshot_open()
        elif hasattr(obj, "snapshot_dict"):
            positions = obj.snapshot_dict()
        elif hasattr(obj, "open_positions"):
            positions = getattr(obj, "open_positions", None)
        elif isinstance(obj, dict):
            positions = obj

        if isinstance(positions, dict):
            for k, v in positions.items():
                ks = normalize_symbol(k)
                if ks:
                    symbols.append(ks)
                    continue

                if isinstance(v, dict):
                    vs = normalize_symbol(
                        v.get("symbol")
                        or v.get("Symbol")
                        or v.get("code")
                        or v.get("ticker")
                    )
                    if vs:
                        symbols.append(vs)

        elif isinstance(positions, (list, tuple)):
            for item in positions:
                if isinstance(item, dict):
                    s = normalize_symbol(
                        item.get("symbol")
                        or item.get("Symbol")
                        or item.get("code")
                        or item.get("ticker")
                    )
                    if s:
                        symbols.append(s)
                else:
                    s = normalize_symbol(item)
                    if s:
                        symbols.append(s)

    except Exception:
        logger.debug("[SUB MANAGER PRIORITY] collect position symbols failed", exc_info=True)

    return dedupe_keep_order(symbols)


def collect_open_position_symbols() -> list[str]:
    gd = safe_get_global_data()
    if gd is None:
        return []

    symbols: list[str] = []

    try:
        pos_obj = safe_getattr(gd, "positions", None)
        symbols.extend(_collect_position_symbols_from_obj(pos_obj))

        for attr in (
            "open_positions",
            "positions_dict",
            "holding_positions",
            "current_positions",
        ):
            v = safe_getattr(gd, attr, None)
            symbols.extend(_collect_position_symbols_from_obj(v))

    except Exception:
        logger.exception("[SUB MANAGER PRIORITY] failed to collect open position symbols")

    return dedupe_keep_order(symbols)


def collect_pending_order_symbols() -> list[str]:
    gd = safe_get_global_data()
    if gd is None:
        return []

    symbols: list[str] = []

    try:
        for attr in (
            "pending_order_symbols",
            "pending_orders",
            "active_order_symbols",
            "active_orders",
            "order_symbols",
            "entry_order_symbols",
            "buy_order_symbols",
        ):
            v = safe_getattr(gd, attr, None)

            if isinstance(v, dict):
                symbols.extend(normalize_symbols(v.keys()))
                for item in v.values():
                    if isinstance(item, dict):
                        symbols.extend(
                            normalize_symbols(
                                [
                                    item.get("symbol"),
                                    item.get("Symbol"),
                                    item.get("code"),
                                    item.get("ticker"),
                                ]
                            )
                        )
            else:
                symbols.extend(normalize_symbols(v))

    except Exception:
        logger.exception("[SUB MANAGER PRIORITY] failed to collect pending order symbols")

    return dedupe_keep_order(symbols)


def collect_recent_entry_symbols() -> list[str]:
    gd = safe_get_global_data()
    if gd is None:
        return []

    symbols: list[str] = []

    try:
        for attr in (
            "recent_entry_symbols",
            "entry_symbols",
            "last_entry_symbols",
            "recent_buy_symbols",
            "ai_entry_symbols",
        ):
            v = safe_getattr(gd, attr, None)
            symbols.extend(normalize_symbols(v))

    except Exception:
        logger.exception("[SUB MANAGER PRIORITY] failed to collect recent entry symbols")

    return dedupe_keep_order(symbols)


def collect_priority_push_symbols() -> list[str]:
    """
    PUSH購読から落としてはいけない銘柄を収集する。
    """
    position_symbols = collect_open_position_symbols()
    pending_symbols = collect_pending_order_symbols()
    recent_entry_symbols = collect_recent_entry_symbols()

    priority = dedupe_keep_order(
        list(position_symbols)
        + list(pending_symbols)
        + list(recent_entry_symbols)
    )

    logger.info(
        "[SUB MANAGER PRIORITY] position=%d pending=%d recent=%d total=%d symbols=%s",
        len(position_symbols),
        len(pending_symbols),
        len(recent_entry_symbols),
        len(priority),
        priority[:50],
    )

    gd = safe_get_global_data()
    if gd is not None:
        try:
            safe_setattr(gd, "position_push_symbols", list(position_symbols))
            safe_setattr(gd, "pending_order_push_symbols", list(pending_symbols))
            safe_setattr(gd, "recent_entry_push_symbols", list(recent_entry_symbols))
            safe_setattr(gd, "protected_push_symbols", list(priority))
            safe_setattr(gd, "priority_push_symbols", list(priority))
        except Exception:
            logger.debug("[SUB MANAGER PRIORITY] failed to save priority symbols", exc_info=True)

    return priority


__all__ = [
    "collect_open_position_symbols",
    "collect_pending_order_symbols",
    "collect_recent_entry_symbols",
    "collect_priority_push_symbols",
]
