# ============================================================
# File   : trading/ranking/active_symbols/liquidity.py
# Version: Ver1.6-FIX-SUMMARY-PRICE-SQL-STRICT-MISSING-PRICE
# ------------------------------------------------------------
# Purpose:
#   - PUSH登録候補の流動性/価格フィルタ
#   - 低位株や極端に流動性が低い銘柄を除外する
#   - 監視銘柄を価格条件内に制限する
#
# Ver1.6:
#   - summary DB 価格補完SQLの閉じ括弧不足で
#     "incomplete input" になっていた不具合を修正。
#   - 価格補完ができなかった銘柄を既定で除外する。
#     ただし寄前SBIで価格がまだ取れない場合のみ、envで緩和可能。
#   - ACTIVE_FINAL_PRICE_GUARD_ALLOW_UNKNOWN_PRICE=1 で従来のfail-openへ戻せる。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
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


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path() -> str:
    base = os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_today()}.db"))


def _qident(name: Any) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None
    except Exception:
        return False


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({_qident(table)})").fetchall()}
    except Exception:
        return set()


def _summary_price_fallback_enabled() -> bool:
    if not _env_bool("ACTIVE_SUMMARY_PRICE_FALLBACK_ENABLED", True):
        return False
    if _is_main_py_process() and not _env_bool("ACTIVE_SUMMARY_PRICE_FALLBACK_RUN_IN_MAIN", False):
        return False
    return True


def _summary_price_fallback_map(symbols: Iterable[str]) -> Dict[str, Dict[str, float]]:
    """
    寄前SBIやランキング側に価格列が無い場合の最終価格補完。

    main.py起動時にNAS SQLiteへ100銘柄×複数テーブルのSELECTを行うと
    起動停止に見えるため、既定ではmain.pyではスキップする。
    main_database.py側や手動検証では env で有効化できる。
    """
    cleaned = [normalize_symbol(s) for s in dedupe_keep_order(symbols)]
    cleaned = [s for s in cleaned if s]
    if not cleaned:
        return {}

    if not _summary_price_fallback_enabled():
        logger.warning(
            "[ACTIVE SUMMARY PRICE FALLBACK] skipped symbols=%d reason=disabled_or_main_process run_in_main=%s",
            len(cleaned),
            os.getenv("ACTIVE_SUMMARY_PRICE_FALLBACK_RUN_IN_MAIN"),
        )
        return {}

    path = _summary_db_path()
    if not path or not Path(path).exists():
        logger.warning("[ACTIVE SUMMARY PRICE FALLBACK] db not found path=%s symbols=%d", path, len(cleaned))
        return {}

    timeout_sec = max(0.05, _env_float("ACTIVE_SUMMARY_PRICE_FALLBACK_TIMEOUT_SEC", 0.35))
    busy_ms = int(max(50.0, _env_float("ACTIVE_SUMMARY_PRICE_FALLBACK_BUSY_TIMEOUT_MS", 300.0)))
    t0 = time.monotonic()
    out: Dict[str, Dict[str, float]] = {}
    try:
        with sqlite3.connect(path, timeout=timeout_sec) as conn:
            conn.execute(f"PRAGMA busy_timeout={busy_ms};")
            for table in ("stock_summary_1min", "stock_summary_3min", "stock_summary_5min"):
                if len(out) >= len(cleaned):
                    break
                if not _table_exists(conn, table):
                    continue
                cols = _table_cols(conn, table)
                if "symbol" not in cols:
                    continue

                if "datetime" in cols:
                    dt_expr = _qident("datetime")
                    dt_expr_t2 = f"t2.{_qident('datetime')}"
                elif "date" in cols and "time" in cols:
                    dt_expr = f"({_qident('date')} || ' ' || {_qident('time')})"
                    dt_expr_t2 = f"(t2.{_qident('date')} || ' ' || t2.{_qident('time')})"
                else:
                    continue

                price_col = None
                for c in ("current_price", "price", "close", "close_price"):
                    if c in cols:
                        price_col = c
                        break
                if not price_col:
                    continue

                remain = [s for s in cleaned if s not in out]
                if not remain:
                    break
                placeholders = ",".join(["?"] * len(remain))
                table_q = _qident(table)
                symbol_q = _qident("symbol")
                # symbolごとの最新行を1回のSQLで取得する。
                # Ver1.6: 旧SQLはサブクエリの閉じ括弧が無く "incomplete input" になっていた。
                sql = f"""
                    SELECT CAST({symbol_q} AS TEXT) AS symbol,
                           {_qident(price_col)} AS price,
                           {dt_expr} AS dtv
                    FROM {table_q}
                    WHERE CAST({symbol_q} AS TEXT) IN ({placeholders})
                      AND {dt_expr} = (
                          SELECT MAX({dt_expr_t2})
                          FROM {table_q} t2
                          WHERE CAST(t2.{symbol_q} AS TEXT) = CAST({table_q}.{symbol_q} AS TEXT)
                      )
                """
                try:
                    rows = conn.execute(sql, remain).fetchall()
                except Exception as e:
                    logger.warning("[ACTIVE SUMMARY PRICE FALLBACK] bulk select skipped table=%s err=%s", table, e, exc_info=False)
                    continue

                for row in rows or []:
                    sym = normalize_symbol(row[0])
                    price = to_float(row[1], 0.0)
                    if sym and price > 0 and sym not in out:
                        out[sym] = {
                            "current_price": price,
                            "price": price,
                            "close": price,
                            "summary_price_table": table,
                        }
        logger.warning(
            "[ACTIVE SUMMARY PRICE FALLBACK] loaded symbols=%d hit=%d missing=%d elapsed=%.3fs path=%s",
            len(cleaned),
            len(out),
            max(0, len(cleaned) - len(out)),
            time.monotonic() - t0,
            path,
        )
        return out
    except sqlite3.OperationalError as e:
        logger.warning("[ACTIVE SUMMARY PRICE FALLBACK] sqlite skipped path=%s symbols=%d err=%s", path, len(cleaned), e, exc_info=False)
        return {}
    except Exception:
        logger.exception("[ACTIVE SUMMARY PRICE FALLBACK] failed path=%s symbols=%d", path, len(cleaned))
        return {}


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


def _allow_unknown_price(*, premarket_mode: bool) -> bool:
    # 旧挙動へ戻したい場合の逃げ道。
    if _env_bool("ACTIVE_FINAL_PRICE_GUARD_ALLOW_UNKNOWN_PRICE", False):
        return True
    # 寄前SBIはCSVに価格列が無い場合があるため、必要なら明示的に許可できる。
    if premarket_mode and _env_bool("ACTIVE_PREMARKET_ALLOW_NO_PRICE", False):
        return True
    return False


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

    logger.warning(
        "[ACTIVE FINAL PRICE GUARD] start symbols=%d premarket=%s fallback_enabled=%s run_in_main=%s allow_unknown_price=%s",
        len(items),
        premarket_mode,
        _summary_price_fallback_enabled(),
        os.getenv("ACTIVE_SUMMARY_PRICE_FALLBACK_RUN_IN_MAIN"),
        _allow_unknown_price(premarket_mode=premarket_mode),
    )

    # 価格情報が無い候補は summary DB から直近価格を補完する。
    missing_price_symbols = []
    for s in items:
        sym = normalize_symbol(s)
        if not sym or sym in protected:
            continue
        info = liquidity_map.get(sym)
        if not _has_positive_value(info, ("current_price", "price", "close", "last_price", "close_price", "現在値")):
            missing_price_symbols.append(sym)
    if missing_price_symbols:
        fallback = _summary_price_fallback_map(missing_price_symbols)
        for sym, info in fallback.items():
            merged = dict(liquidity_map.get(sym, {}) or {})
            merged.update(info)
            liquidity_map[sym] = merged

    kept: List[str] = []
    removed: List[str] = []
    missing_info_kept: List[str] = []
    missing_info_removed: List[str] = []
    price_guarded: List[str] = []
    allow_unknown = _allow_unknown_price(premarket_mode=premarket_mode)

    for s in items:
        sym = normalize_symbol(s)
        if not sym:
            continue

        if sym in protected:
            kept.append(sym)
            continue

        info = liquidity_map.get(sym)

        if not _has_usable_liquidity_info(info):
            if allow_unknown:
                kept.append(sym)
                missing_info_kept.append(sym)
            else:
                removed.append(sym)
                missing_info_removed.append(sym)
            continue

        assert info is not None
        price = _get_price(info)
        if price <= 0 and not allow_unknown:
            removed.append(sym)
            missing_info_removed.append(sym)
            continue

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

    if removed or missing_info_kept or missing_info_removed or premarket_mode:
        logger.warning(
            "[ACTIVE FINAL PRICE GUARD] before=%d after=%d removed=%d missing_info_kept=%d missing_info_removed=%d "
            "premarket=%s allow_unknown_price=%s price_guarded=%d min_price=%.1f max_price=%.1f removed_head=%s missing_kept_head=%s missing_removed_head=%s",
            len(items),
            len(kept),
            len(removed),
            len(missing_info_kept),
            len(missing_info_removed),
            premarket_mode,
            allow_unknown,
            len(price_guarded),
            MIN_PRICE,
            MAX_PRICE,
            removed[:30],
            missing_info_kept[:30],
            missing_info_removed[:30],
        )
    else:
        logger.info(
            "[ACTIVE FINAL PRICE GUARD] before=%d after=%d removed=0 missing_info_kept=0 missing_info_removed=0 premarket=%s price_guarded=%d min_price=%.1f max_price=%.1f",
            len(items),
            len(kept),
            premarket_mode,
            len(price_guarded),
            MIN_PRICE,
            MAX_PRICE,
        )

    return dedupe_keep_order(kept)
