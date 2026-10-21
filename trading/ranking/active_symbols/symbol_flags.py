# ============================================================
# File   : trading/ranking/active_symbols/symbol_flags.py
# Version: Ver1.1-ACTIVE-SYMBOLS-SYMBOL-FLAGS-CREDIT-TYPE-CACHE
# ============================================================
from __future__ import annotations
import logging, os, sqlite3
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
            wanted_cols = [
                "symbol",
                "symbolname",
                "buy_target",
                "sell_target",
                "is_etf",
                "market",
                "market_type",
                "ats_ok",
                "short_ok",
                "is_margin",
                "credit_type",
                "is_attention",
                "updated_at",
            ]
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
        logger.info(
            "[ACTIVE FLAGS] eligible loaded db=%s eligible=%d buy=%s sell=%s exclude_etf=%s info_cols=%s",
            p,
            len(eligible),
            ACTIVE_ALLOW_BUY_TARGET,
            ACTIVE_ALLOW_SELL_TARGET,
            ACTIVE_EXCLUDE_ETF,
            select_cols,
        )
        return eligible, info_map
    except Exception:
        logger.exception("[ACTIVE FLAGS] load failed db=%s", p)
        return eligible, info_map


def _unknown_allow() -> bool:
    """symbol_flags DB に載っていない銘柄を通すか (既定 True)。

    2026-09-29: この DB は optional/batch/update_symbol_flags.py が
    daily_watchlist から作るバッチ生成物で、updated_at が全 4,509 件
    **2026-03-30** のまま (ファイル mtime も 5/22)。
    そのため 2026-04 以降に上場した銘柄は「登録が無い」だけで、
    旧実装はそれを「対象外」として落としていた。

    実際に売買できる反証がある: 621A は 2026-09-28 に日計り信用で
    建てて返済まで完了している (取引明細 -2,298 円) が、DB には無い。
    627A / 625A / 634A / 646A / 593A / 604A / 259A / 619A も同様に
    「DB に無い」だけ。これらは値上がり率上位に頻繁に出る銘柄群なので、
    落とすと最も急騰する銘柄を恒久的に監視対象外にしてしまう。

    一方 396A / 485A は DB にあって buy_target=0 / sell_target=0 なので、
    こちらは明示的に対象外として落とす。

    ACTIVE_FLAGS_UNKNOWN_ALLOW=0 で従来動作 (DB に無ければ落とす) に戻る。
    DB を作り直したらこの区別は不要になる。
    """
    v = os.environ.get("ACTIVE_FLAGS_UNKNOWN_ALLOW")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() not in ("0", "false", "no", "off", "ng", "disabled")


_KNOWN_CACHE: Dict[str, Any] = {"ts": 0.0, "known": None}


def load_symbol_flags_known_symbols(*, db_path: str | Path = SYMBOL_FLAGS_DB, table: str = "symbol_flags") -> Set[str]:
    """DB に行が存在する銘柄 (対象/対象外を問わない)。読めなければ空集合。"""
    import time

    now = time.time()
    cached = _KNOWN_CACHE.get("known")
    if cached is not None and now - float(_KNOWN_CACHE.get("ts") or 0.0) < 300.0:
        return cached
    known: Set[str] = set()
    try:
        p = Path(str(db_path))
        if _path_exists(p):
            with _connect_sqlite(p) as conn:
                if _table_exists(conn, table):
                    for r in conn.execute(f"SELECT symbol FROM {table}"):
                        s = normalize_symbol(r[0] if not hasattr(r, "keys") else r["symbol"])
                        if s:
                            known.add(s)
    except Exception:
        logger.debug("[ACTIVE FLAGS] known symbols load failed", exc_info=True)
        known = set()
    _KNOWN_CACHE["known"] = known
    _KNOWN_CACHE["ts"] = now
    return known


def filter_by_symbol_flags(symbols: Iterable[Any], *, eligible_symbols: Optional[Set[str]] = None, context: str = "") -> List[str]:
    cleaned = dedupe_keep_order(symbols)
    if not ACTIVE_REQUIRE_SYMBOL_FLAGS:
        return cleaned
    eligible = eligible_symbols if eligible_symbols is not None else load_symbol_flags_eligible_symbols()[0]
    if not eligible:
        logger.warning("[ACTIVE FLAGS] eligible empty context=%s before=%d -> return empty", context, len(cleaned))
        return []
    unknown_allow = _unknown_allow()
    known = load_symbol_flags_known_symbols() if unknown_allow else set()
    kept, removed, unknown_kept = [], [], []
    for s in cleaned:
        if s in eligible:
            kept.append(s)
        elif unknown_allow and known and s not in known:
            # DB に登録が無いだけ。明示的な対象外ではないので通す。
            kept.append(s)
            unknown_kept.append(s)
        else:
            removed.append(s)
    logger.info(
        "[ACTIVE FLAGS] context=%s before=%d after=%d removed=%d unknown_kept=%d "
        "known=%d unknown_allow=%s removed_head=%s unknown_head=%s",
        context, len(cleaned), len(kept), len(removed), len(unknown_kept),
        len(known), unknown_allow, removed[:30], unknown_kept[:20],
    )
    return kept
