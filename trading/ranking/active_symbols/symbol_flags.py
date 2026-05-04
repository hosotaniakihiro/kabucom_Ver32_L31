# ============================================================
# File   : trading/ranking/active_symbols/symbol_flags.py
# Version: Ver1.0-ACTIVE-SYMBOLS-SYMBOL-FLAGS
# ============================================================
from __future__ import annotations
import logging, sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from .config import ACTIVE_ALLOW_BUY_TARGET, ACTIVE_ALLOW_SELL_TARGET, ACTIVE_EXCLUDE_ETF, ACTIVE_REQUIRE_SYMBOL_FLAGS, SYMBOL_FLAGS_DB
from .normalize import dedupe_keep_order, normalize_symbol

logger = logging.getLogger(__name__)


def _path_exists(path: str | Path) -> bool:
    try:
        return Path(str(path)).exists()
    except Exception:
        return False


def _connect_sqlite(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None
    except Exception:
        return False


def _get_table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [str(r["name"]) for r in rows]
    except Exception:
        return []


def load_symbol_flags_eligible_symbols(*, db_path: str | Path = SYMBOL_FLAGS_DB, table: str = "symbol_flags") -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
    eligible: Set[str] = set()
    info_map: Dict[str, Dict[str, Any]] = {}
    if not ACTIVE_REQUIRE_SYMBOL_FLAGS:
        logger.info("[ACTIVE FLAGS] symbol_flags check disabled")
        return eligible, info_map
    p = Path(str(db_path))
    if not _path_exists(p):
        logger.warning("[ACTIVE FLAGS] symbol_flags db not found path=%s", p)
        return eligible, info_map
    try:
        with _connect_sqlite(p) as conn:
            if not _table_exists(conn, table):
                logger.warning("[ACTIVE FLAGS] table not found db=%s table=%s", p, table)
                return eligible, info_map
            cols = _get_table_columns(conn, table)
            wanted_cols = ["symbol", "symbolname", "buy_target", "sell_target", "is_etf", "market", "market_type", "ats_ok", "short_ok", "is_margin"]
            select_cols = [c for c in wanted_cols if c in cols]
            if "symbol" not in select_cols:
                logger.warning("[ACTIVE FLAGS] symbol column missing db=%s cols=%s", p, cols)
                return eligible, info_map
            rows = conn.execute(f"SELECT {', '.join(select_cols)} FROM {table}").fetchall()
            for r in rows:
                d = {k: r[k] for k in r.keys()}
                sym = normalize_symbol(d.get("symbol"))
                if not sym:
                    continue
                is_etf = int(d.get("is_etf") or 0) if "is_etf" in d else 0
                buy_target = int(d.get("buy_target") or 0) if "buy_target" in d else 0
                sell_target = int(d.get("sell_target") or 0) if "sell_target" in d else 0
                if ACTIVE_EXCLUDE_ETF and is_etf == 1:
                    continue
                ok_side = False
                if ACTIVE_ALLOW_BUY_TARGET and buy_target == 1:
                    ok_side = True
                if ACTIVE_ALLOW_SELL_TARGET and sell_target == 1:
                    ok_side = True
                if not ok_side:
                    continue
                eligible.add(sym)
                info_map[sym] = d
        logger.info("[ACTIVE FLAGS] eligible loaded db=%s eligible=%d buy=%s sell=%s exclude_etf=%s", p, len(eligible), ACTIVE_ALLOW_BUY_TARGET, ACTIVE_ALLOW_SELL_TARGET, ACTIVE_EXCLUDE_ETF)
        return eligible, info_map
    except Exception:
        logger.exception("[ACTIVE FLAGS] load failed db=%s", p)
        return eligible, info_map


def filter_by_symbol_flags(symbols: Iterable[Any], *, eligible_symbols: Optional[Set[str]] = None, context: str = "") -> List[str]:
    cleaned = dedupe_keep_order(symbols)
    if not ACTIVE_REQUIRE_SYMBOL_FLAGS:
        return cleaned
    eligible = eligible_symbols if eligible_symbols is not None else load_symbol_flags_eligible_symbols()[0]
    if not eligible:
        logger.warning("[ACTIVE FLAGS] eligible empty context=%s before=%d -> return empty", context, len(cleaned))
        return []
    kept, removed = [], []
    for s in cleaned:
        if s in eligible:
            kept.append(s)
        else:
            removed.append(s)
    logger.info("[ACTIVE FLAGS] context=%s before=%d after=%d removed=%d removed_head=%s", context, len(cleaned), len(kept), len(removed), removed[:30])
    return kept
