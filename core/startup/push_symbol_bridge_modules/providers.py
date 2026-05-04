# ============================================================
# File   : core/startup/push_symbol_bridge_modules/providers.py
# Version: PRODUCTION-STABLE-REV3.0
# ------------------------------------------------------------
# Purpose:
#   PUSH監視候補100銘柄を各ソースから解決する。
#
# Provider priority:
#   1. trading.ranking.active_symbol_manager
#   2. global_data
#   3. optional daily_watchlist
#   4. symbol_flags DB
# ============================================================

from __future__ import annotations

import importlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Callable, List, Sequence, Tuple

from .constants import (
    DEFAULT_MAX_SYMBOLS,
    DEFAULT_OPTIONAL_DB,
    DEFAULT_SYMBOL_FLAGS_DB,
)
from .import_utils import get_global_data, safe_call
from .normalize import clean_symbols

logger = logging.getLogger(__name__)


def resolve_from_active_symbol_manager(*, limit: int) -> List[str]:
    module_name = "trading.ranking.active_symbol_manager"

    func_names = (
        "get_active_symbols",
        "get_monitor_symbols",
        "get_push_symbols",
        "get_register_symbols",
        "get_current_active_symbols",
        "load_active_symbols",
    )

    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return []

    for func_name in func_names:
        fn = getattr(mod, func_name, None)
        if not callable(fn):
            continue

        src = safe_call(fn, limit=limit)
        symbols = clean_symbols(src, limit=limit)

        logger.info(
            "[PUSH SYMBOL BRIDGE] provider=%s.%s real=%d head=%s",
            module_name,
            func_name,
            len(symbols),
            symbols[:10],
        )

        if symbols:
            return symbols

    attr_names = (
        "active_symbols",
        "monitor_symbols",
        "push_symbols",
        "register_symbols",
        "CURRENT_ACTIVE_SYMBOLS",
        "ACTIVE_SYMBOLS",
        "MONITOR_SYMBOLS",
    )

    for attr_name in attr_names:
        src = getattr(mod, attr_name, None)
        if src is None:
            continue

        symbols = clean_symbols(src, limit=limit)

        logger.info(
            "[PUSH SYMBOL BRIDGE] provider=%s.%s real=%d head=%s",
            module_name,
            attr_name,
            len(symbols),
            symbols[:10],
        )

        if symbols:
            return symbols

    return []


def resolve_from_global_data(*, limit: int) -> List[str]:
    gd = get_global_data()
    if gd is None:
        return []

    names = (
        "candidate_push_symbols",
        "push_candidate_symbols",
        "push_symbols_100",
        "monitor_symbols",
        "active_symbols",
        "daily_watchlist_symbols",
        # 互換fallback
        "push_symbols",
        "register_symbols",
        "ats_targets",
        "ats_register_targets",
    )

    for name in names:
        try:
            src = getattr(gd, name, None)
        except Exception:
            src = None

        if src is None:
            continue

        if callable(src):
            src = safe_call(src, limit=limit)

        symbols = clean_symbols(src, limit=limit)

        logger.info(
            "[PUSH SYMBOL BRIDGE] provider=global_data.%s real=%d head=%s",
            name,
            len(symbols),
            symbols[:10],
        )

        if symbols:
            return symbols

    getter_names = (
        "get_candidate_push_symbols",
        "get_push_candidate_symbols",
        "get_active_symbols",
        "get_monitor_symbols",
        "get_push_symbols",
        "get_register_symbols",
        "get_ats_targets",
        "get_ats_register_targets",
    )

    for name in getter_names:
        try:
            fn = getattr(gd, name, None)
        except Exception:
            fn = None

        if not callable(fn):
            continue

        src = safe_call(fn, limit=limit)
        symbols = clean_symbols(src, limit=limit)

        logger.info(
            "[PUSH SYMBOL BRIDGE] provider=global_data.%s() real=%d head=%s",
            name,
            len(symbols),
            symbols[:10],
        )

        if symbols:
            return symbols

    return []


def read_sql_symbols(
    db_path: Path,
    *,
    sql_candidates: Sequence[str],
    limit: int,
    source_name: str,
) -> List[str]:
    if not db_path.exists():
        logger.warning(
            "[PUSH SYMBOL BRIDGE] db not found source=%s path=%s",
            source_name,
            db_path,
        )
        return []

    try:
        with sqlite3.connect(str(db_path), timeout=10) as con:
            con.row_factory = sqlite3.Row

            for sql in sql_candidates:
                try:
                    rows = con.execute(sql, {"limit": int(limit)}).fetchall()
                except Exception:
                    continue

                vals = []
                for r in rows:
                    try:
                        vals.append(r["symbol"])
                    except Exception:
                        try:
                            vals.append(r[0])
                        except Exception:
                            pass

                symbols = clean_symbols(vals, limit=limit)

                logger.info(
                    "[PUSH SYMBOL BRIDGE] provider=%s sql real=%d head=%s",
                    source_name,
                    len(symbols),
                    symbols[:10],
                )

                if symbols:
                    return symbols

    except Exception:
        logger.exception(
            "[PUSH SYMBOL BRIDGE] read sql symbols failed source=%s path=%s",
            source_name,
            db_path,
        )

    return []


def resolve_from_optional_daily_watchlist(*, limit: int) -> List[str]:
    db_path = Path(os.environ.get("OPTIONAL_DB_PATH", str(DEFAULT_OPTIONAL_DB)))

    sql_candidates = (
        """
        SELECT symbol
        FROM daily_watchlist
        WHERE symbol IS NOT NULL
        ORDER BY rowid
        LIMIT :limit
        """,
        """
        SELECT symbol
        FROM daily_watchlist_symbols
        WHERE symbol IS NOT NULL
        ORDER BY rowid
        LIMIT :limit
        """,
        """
        SELECT symbol
        FROM watchlist
        WHERE symbol IS NOT NULL
        ORDER BY rowid
        LIMIT :limit
        """,
    )

    return read_sql_symbols(
        db_path,
        sql_candidates=sql_candidates,
        limit=limit,
        source_name="optional_daily_watchlist",
    )


def resolve_from_symbol_flags(*, limit: int) -> List[str]:
    db_path = Path(os.environ.get("SYMBOL_FLAGS_DB_PATH", str(DEFAULT_SYMBOL_FLAGS_DB)))

    sql_candidates = (
        """
        SELECT symbol
        FROM symbol_flags
        WHERE symbol IS NOT NULL
          AND COALESCE(buy_target, 0) = 1
          AND COALESCE(is_etf, 0) = 0
          AND COALESCE(market_type, '') IN ('プライム', 'スタンダード', 'グロース')
        ORDER BY symbol
        LIMIT :limit
        """,
        """
        SELECT symbol
        FROM symbol_flags
        WHERE symbol IS NOT NULL
          AND COALESCE(is_etf, 0) = 0
        ORDER BY symbol
        LIMIT :limit
        """,
        """
        SELECT symbol
        FROM symbol_flags
        WHERE symbol IS NOT NULL
        ORDER BY symbol
        LIMIT :limit
        """,
    )

    return read_sql_symbols(
        db_path,
        sql_candidates=sql_candidates,
        limit=limit,
        source_name="symbol_flags",
    )


def resolve_real_push_symbols(*, limit: int = DEFAULT_MAX_SYMBOLS) -> List[str]:
    providers: Tuple[Tuple[str, Callable[..., List[str]]], ...] = (
        ("active_symbol_manager", resolve_from_active_symbol_manager),
        ("global_data", resolve_from_global_data),
        ("optional_daily_watchlist", resolve_from_optional_daily_watchlist),
        ("symbol_flags", resolve_from_symbol_flags),
    )

    for name, provider in providers:
        try:
            symbols = provider(limit=limit)
        except Exception:
            logger.exception("[PUSH SYMBOL BRIDGE] provider failed name=%s", name)
            symbols = []

        symbols = clean_symbols(symbols, limit=limit)

        logger.info(
            "[PUSH SYMBOL BRIDGE] resolved candidate provider=%s real=%d head=%s",
            name,
            len(symbols),
            symbols[:10],
        )

        if symbols:
            return symbols

    logger.error("[PUSH SYMBOL BRIDGE] failed to resolve real push symbols")
    return []


__all__ = [
    "resolve_from_active_symbol_manager",
    "resolve_from_global_data",
    "resolve_from_optional_daily_watchlist",
    "resolve_from_symbol_flags",
    "resolve_real_push_symbols",
]
