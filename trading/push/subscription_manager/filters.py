# ============================================================
# File   : trading/push/subscription_manager/filters.py
# Function:
#   - common stock filter
#   - symbol_flags.db による buy/sell target 抽出
#   - freshness filter
#   - global_data / push timestamp を使った候補絞り込み
# ------------------------------------------------------------
# Notes:
#   - ETF/ETN/REIT/FUND を除外
#   - market_type を プライム / スタンダード / グロース に制限
#   - sell_target は 貸借銘柄 のみを残す
#   - freshness は古い銘柄を push 候補から除外する
# ============================================================

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, List, Optional, Sequence, Set, Tuple

from .globals_access import safe_get_global_data, safe_getattr
from .symbols import dedupe_keep_order, normalize_symbol, safe_str

logger = logging.getLogger(__name__)

SYMBOL_FLAGS_DB_PATH = r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db"
COMMON_MARKETS = ("プライム", "スタンダード", "グロース")
DEFAULT_REFRESH_INTERVAL_SEC = 30.0


# ============================================================
# basic helpers
# ============================================================

def safe_bool_like(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "t", "yes", "y", "on"):
            return True
        if s in ("0", "false", "f", "no", "n", "off", ""):
            return False
    return default


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


# ============================================================
# symbol_flags target sets
# ============================================================

def read_symbol_flags_target_sets(
    db_path: Optional[str] = None,
) -> tuple[Set[str], Set[str]]:
    """
    symbol_flags から buy_target / sell_target 集合を読む。

    buy_target=1 -> buy 候補
    sell_target=1 かつ credit_type='貸借銘柄' -> sell 候補
    ETF/ETN/REIT/FUND は除外する
    """
    db_path = db_path or SYMBOL_FLAGS_DB_PATH

    buy_set: Set[str] = set()
    sell_set: Set[str] = set()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("PRAGMA table_info(symbol_flags)").fetchall()
        cols = {str(r["name"]) for r in rows if "name" in r.keys()}

        if not cols:
            raise RuntimeError("symbol_flags schema unavailable")
        if "symbol" not in cols:
            raise RuntimeError("symbol_flags.symbol column missing")

        required_any = {"buy_target", "sell_target"}
        if not (required_any & cols):
            raise RuntimeError("symbol_flags.buy_target / sell_target missing")

        select_cols = ["symbol"]
        for c in (
            "market_type",
            "symbolname",
            "buy_target",
            "sell_target",
            "credit_type",
            "is_etf",
            "is_etn",
            "is_reit",
            "is_fund",
        ):
            if c in cols:
                select_cols.append(c)

        sql = f"SELECT {', '.join(select_cols)} FROM symbol_flags"
        db_rows = conn.execute(sql).fetchall()

    for row in db_rows:
        symbol = normalize_symbol(row["symbol"])
        if not symbol:
            continue

        market_type = safe_str(row["market_type"]) if "market_type" in row.keys() else ""
        symbolname = safe_str(row["symbolname"]) if "symbolname" in row.keys() else ""
        credit_type = safe_str(row["credit_type"]) if "credit_type" in row.keys() else ""

        if market_type and market_type not in COMMON_MARKETS:
            continue

        is_etf = safe_bool_like(row["is_etf"]) if "is_etf" in row.keys() else False
        is_etn = safe_bool_like(row["is_etn"]) if "is_etn" in row.keys() else False
        is_reit = safe_bool_like(row["is_reit"]) if "is_reit" in row.keys() else False
        is_fund = safe_bool_like(row["is_fund"]) if "is_fund" in row.keys() else False

        text_blob = f"{market_type} {symbolname}".lower()
        bad_keywords = [
            "etf", "etn", "reit", "fund", "指数", "連動",
            "インデックス", "レバ", "ダブル", "ベア", "ブル",
            "インバース", "j-reit",
        ]

        if is_etf or is_etn or is_reit or is_fund or any(k.lower() in text_blob for k in bad_keywords):
            continue

        buy_target = safe_bool_like(row["buy_target"]) if "buy_target" in row.keys() else False
        sell_target = safe_bool_like(row["sell_target"]) if "sell_target" in row.keys() else False

        if buy_target:
            buy_set.add(symbol)

        if sell_target and credit_type == "貸借銘柄":
            sell_set.add(symbol)

    return buy_set, sell_set


def filter_by_symbol_flags_targets(
    symbols: Sequence[str],
    db_path: Optional[str] = None,
) -> tuple[List[str], List[str]]:
    if not symbols:
        return [], []

    db_path = db_path or SYMBOL_FLAGS_DB_PATH
    normalized = [normalize_symbol(x) for x in symbols]
    normalized = [s for s in normalized if s]
    normalized = dedupe_keep_order(normalized)

    try:
        buy_set, sell_set = read_symbol_flags_target_sets(db_path=db_path)
    except Exception:
        logger.exception("[SUB MANAGER] symbol_flags target filter failed")
        return list(normalized), []

    buy_symbols = [s for s in normalized if s in buy_set]
    sell_symbols = [s for s in normalized if s in sell_set]

    buy_symbols = dedupe_keep_order(buy_symbols)
    sell_symbols = dedupe_keep_order(sell_symbols)

    logger.info(
        "[SUB MANAGER] target filter via symbol_flags buy=%d sell=%d raw=%d db=%s",
        len(buy_symbols),
        len(sell_symbols),
        len(normalized),
        db_path,
    )
    return buy_symbols, sell_symbols


# ============================================================
# common stock filter
# ============================================================

def read_symbol_flags_keep_set(db_path: str) -> Set[str]:
    keep: Set[str] = set()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("PRAGMA table_info(symbol_flags)").fetchall()
        cols = {str(r["name"]) for r in rows if "name" in r.keys()}

        if not cols:
            raise RuntimeError("symbol_flags schema unavailable")
        if "symbol" not in cols:
            raise RuntimeError("symbol_flags.symbol column missing")

        select_cols = ["symbol"]
        optional_cols = [
            "market_type",
            "is_etf",
            "is_etn",
            "is_reit",
            "is_fund",
            "security_type",
            "product_category",
            "instrument_type",
            "category",
            "name",
            "symbolname",
            "display_name",
        ]
        for c in optional_cols:
            if c in cols:
                select_cols.append(c)

        sql = f"SELECT {', '.join(select_cols)} FROM symbol_flags"
        rows = conn.execute(sql).fetchall()

    for row in rows:
        symbol = normalize_symbol(row["symbol"])
        if not symbol:
            continue

        market_type = safe_str(row["market_type"]) if "market_type" in row.keys() else ""
        if market_type and market_type not in COMMON_MARKETS:
            continue

        is_etf = safe_bool_like(row["is_etf"]) if "is_etf" in row.keys() else False
        is_etn = safe_bool_like(row["is_etn"]) if "is_etn" in row.keys() else False
        is_reit = safe_bool_like(row["is_reit"]) if "is_reit" in row.keys() else False
        is_fund = safe_bool_like(row["is_fund"]) if "is_fund" in row.keys() else False

        if is_etf or is_etn or is_reit or is_fund:
            continue

        text_parts: List[str] = []
        for c in (
            "security_type",
            "product_category",
            "instrument_type",
            "category",
            "name",
            "symbolname",
            "display_name",
        ):
            if c in row.keys():
                text_parts.append(safe_str(row[c]))

        text_blob = " ".join([x for x in text_parts if x]).lower()

        bad_keywords = [
            "etf", "etn", "reit", "fund", "指数", "連動",
            "インデックス", "レバ", "ダブル", "ベア", "ブル",
            "インバース", "j-reit",
        ]
        if any(k.lower() in text_blob for k in bad_keywords):
            continue

        keep.add(symbol)

    return keep


def try_filter_with_utils_market_filter(symbols: Sequence[str]) -> Optional[List[str]]:
    if not symbols:
        return []

    try:
        mod = __import__("utils.market_filter", fromlist=["dummy"])
    except Exception:
        return None

    candidates = [
        "filter_common_stocks",
        "filter_symbols",
        "apply_market_filter",
        "filter_market_symbols",
        "filter_jpx_common_stocks",
    ]

    for fn_name in candidates:
        fn = getattr(mod, fn_name, None)
        if not callable(fn):
            continue

        payload_candidates = [
            {"symbols": list(symbols)},
            {"codes": list(symbols)},
            {"items": list(symbols)},
            {"target_symbols": list(symbols)},
        ]

        for payload in payload_candidates:
            try:
                try:
                    result = fn(**payload)
                except TypeError:
                    result = fn(list(symbols))
            except Exception:
                logger.debug(
                    "[SUB MANAGER] utils.market_filter candidate failed fn=%s payload_keys=%s",
                    fn_name,
                    list(payload.keys()),
                    exc_info=True,
                )
                continue

            if result is None:
                continue

            if isinstance(result, (list, tuple, set)):
                raw = list(result)
            elif isinstance(result, dict):
                raw = list(result.keys())
            else:
                raw = [result]

            out = [normalize_symbol(x) for x in raw]
            out = [s for s in out if s]
            out = dedupe_keep_order(out)

            if out or (isinstance(result, (list, tuple, set)) and len(result) == 0):
                logger.info(
                    "[SUB MANAGER] common stock filter via utils.market_filter.%s in=%d out=%d",
                    fn_name,
                    len(symbols),
                    len(out),
                )
                return out

    return None


def try_filter_with_symbol_flags_db(
    symbols: Sequence[str],
    db_path: Optional[str] = None,
) -> Optional[List[str]]:
    if not symbols:
        return []

    db_path = db_path or SYMBOL_FLAGS_DB_PATH

    try:
        keep = read_symbol_flags_keep_set(db_path)
        out = [s for s in symbols if s in keep]
        out = dedupe_keep_order(out)
        logger.info(
            "[SUB MANAGER] common stock filter via symbol_flags.db in=%d out=%d db=%s",
            len(symbols),
            len(out),
            db_path,
        )
        return out
    except Exception:
        logger.exception("[SUB MANAGER] common stock filter fallback failed")
        return None


def apply_common_stock_filter(
    symbols: Sequence[str],
    db_path: Optional[str] = None,
) -> List[str]:
    if not symbols:
        return []

    db_path = db_path or SYMBOL_FLAGS_DB_PATH

    normalized = [normalize_symbol(x) for x in symbols]
    normalized = [s for s in normalized if s]
    normalized = dedupe_keep_order(normalized)

    try:
        out = try_filter_with_utils_market_filter(normalized)
        if out is not None:
            return dedupe_keep_order(out)
    except Exception:
        logger.exception("[SUB MANAGER] utils.market_filter failed")

    try:
        out = try_filter_with_symbol_flags_db(normalized, db_path=db_path)
        if out is not None:
            return dedupe_keep_order(out)
    except Exception:
        logger.exception("[SUB MANAGER] symbol_flags fallback failed")

    logger.warning(
        "[SUB MANAGER] common stock filter unavailable -> pass through count=%d",
        len(normalized),
    )
    return dedupe_keep_order(normalized)


# ============================================================
# freshness filter
# ============================================================

def candidate_age_seconds_from_global_data(symbol: str) -> Optional[float]:
    gd = safe_get_global_data()
    if gd is None or not symbol:
        return None

    now = time.time()

    candidate_maps: List[Any] = [
        safe_getattr(gd, "last_push_ts_by_symbol", None),
        safe_getattr(gd, "push_last_ts_by_symbol", None),
        safe_getattr(gd, "last_message_ts_by_symbol", None),
        safe_getattr(gd, "symbol_last_seen_ts", None),
    ]

    for mp in candidate_maps:
        try:
            if isinstance(mp, dict) and symbol in mp:
                ts = safe_float(mp.get(symbol), 0.0)
                if ts > 0:
                    return max(0.0, now - ts)
        except Exception:
            continue

    return None


def apply_freshness_filter(symbols: Sequence[str]) -> List[str]:
    if not symbols:
        return []

    filtered: List[str] = []
    dropped: List[Tuple[str, float]] = []

    for s in symbols:
        age = candidate_age_seconds_from_global_data(s)
        if age is None:
            filtered.append(s)
            continue

        if age > (DEFAULT_REFRESH_INTERVAL_SEC * 6.0):
            dropped.append((s, age))
            continue

        filtered.append(s)

    if dropped:
        logger.info(
            "[SUB MANAGER] freshness filter dropped=%d examples=%s",
            len(dropped),
            [f"{s}:{age:.1f}s" for s, age in dropped[:20]],
        )

    return filtered