# ============================================================
# File   : database/crud/crud_yahoo_tracking_state.py
# Version: PRODUCTION-STABLE-REV1.0-YAHOO-TRACKING-STATE-CRUD
# ------------------------------------------------------------
# Purpose:
#   Ranking出現銘柄をYahoo追跡対象として管理するCRUD。
#
# Main use:
#   1. sync_tracking_symbols_from_ranking_db(...)
#      ranking_snapshot_1min から当日一度でも出た銘柄を登録
#   2. get_active_tracking_symbols(...)
#      1分ごとのYahoo差分取得対象を取得
#   3. update_last_yahoo_downloaded_at(...)
#      取得済みウォーターマーク更新
#   4. update_last_summary_calculated_at(...)
#      summary計算済みウォーターマーク更新
#
# Notes:
#   - Download/calculation business logic is intentionally NOT here.
#   - This file only manages DB state and lightweight reads.
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from database.paths.yahoo_paths import get_yahoo_1min_db_path, normalize_trade_date
from database.schema.yahoo_tracking_state_schema import (
    YAHOO_1MIN_TABLE,
    YAHOO_TRACKING_STATE_TABLE,
    ensure_yahoo_schema,
)
from database.sqlite import DEFAULT_BUSY_TIMEOUT_MS, is_lock_error, lock_sleep_seconds, quote_ident
from database.upsert.yahoo_tracking_state_upsert import upsert_yahoo_tracking_state_rows

logger = logging.getLogger(__name__)

MAX_RETRY = 10


def _now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm_symbol(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.endswith(".T"):
        s = s[:-2]
    return s


def _norm_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min).strftime("%Y-%m-%d %H:%M:%S")
    s = str(value).strip()
    if not s:
        return None
    try:
        import pandas as pd  # type: ignore

        x = pd.to_datetime(s, errors="coerce")
        if pd.isna(x):
            return s
        return x.to_pydatetime().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s


def _connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(
        str(db_path),
        timeout=max(10.0, float(DEFAULT_BUSY_TIMEOUT_MS) / 1000.0),
        check_same_thread=False,
        isolation_level=None,
    )
    try:
        con.execute(f"PRAGMA busy_timeout={int(DEFAULT_BUSY_TIMEOUT_MS)}")
        con.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    con.row_factory = sqlite3.Row
    return con


def _execute_write_with_retry(db_path: str | Path, sql: str, params: Sequence[Any]) -> int:
    ensure_yahoo_schema(db_path, ensure_1min=True, ensure_tracking=True)
    last_err: Exception | None = None

    for attempt in range(1, MAX_RETRY + 1):
        con: sqlite3.Connection | None = None
        try:
            con = _connect(db_path)
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(sql, tuple(params))
            con.execute("COMMIT")
            return int(cur.rowcount or 0)
        except Exception as e:
            last_err = e
            try:
                if con is not None:
                    con.execute("ROLLBACK")
            except Exception:
                pass
            if is_lock_error(e) and attempt < MAX_RETRY:
                sleep_s = lock_sleep_seconds(attempt, 0.35)
                logger.warning(
                    "[YAHOO TRACKING CRUD] locked retry db=%s attempt=%s/%s sleep=%.2fs err=%s",
                    db_path,
                    attempt,
                    MAX_RETRY,
                    sleep_s,
                    str(e).splitlines()[0] if str(e) else type(e).__name__,
                )
                time.sleep(sleep_s)
                continue
            logger.exception("[YAHOO TRACKING CRUD] write failed db=%s sql=%s", db_path, sql)
            break
        finally:
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass

    if last_err is not None:
        raise last_err
    return 0


def ensure_tracking_state_db(
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
) -> str:
    return ensure_yahoo_schema(
        db_path or get_yahoo_1min_db_path(trade_date),
        trade_date=trade_date,
        ensure_1min=True,
        ensure_tracking=True,
    )


def _ranking_table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
        return {str(r[1]) for r in rows}
    except Exception:
        return set()


def _ranking_table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def read_today_ranking_symbols(
    *,
    ranking_db_path: str | Path,
    trade_date: Any = None,
    table: str = "ranking_snapshot_1min",
    min_price: float | None = 200.0,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """
    Read distinct symbols that appeared in ranking_snapshot_1min for trade_date.

    Handles common schema variants:
      - datetime / snapshot_time / inserted_at
      - symbolname / name
      - current_price / close_price
      - ranking_type / rank_type
    """
    db = str(ranking_db_path)
    if not os.path.exists(db):
        logger.warning("[YAHOO TRACKING CRUD] ranking db not found path=%s", db)
        return []

    td = normalize_trade_date(trade_date)
    con: sqlite3.Connection | None = None
    try:
        con = _connect(db)
        if not _ranking_table_exists(con, table):
            logger.warning("[YAHOO TRACKING CRUD] ranking table missing db=%s table=%s", db, table)
            return []

        cols = _ranking_table_columns(con, table)
        if "symbol" not in cols:
            logger.warning("[YAHOO TRACKING CRUD] ranking table has no symbol column db=%s table=%s", db, table)
            return []

        dt_col = "datetime" if "datetime" in cols else "snapshot_time" if "snapshot_time" in cols else "inserted_at" if "inserted_at" in cols else None
        name_col = "symbolname" if "symbolname" in cols else "name" if "name" in cols else None
        price_col = "current_price" if "current_price" in cols else "close_price" if "close_price" in cols else "close" if "close" in cols else None
        volume_col = "trading_volume" if "trading_volume" in cols else "volume" if "volume" in cols else None
        ranking_type_col = "ranking_type" if "ranking_type" in cols else "rank_type" if "rank_type" in cols else "type" if "type" in cols else None
        market_col = "market" if "market" in cols else "exchange" if "exchange" in cols else None
        rank_col = "rank" if "rank" in cols else None

        select_parts = ["symbol"]
        if name_col:
            select_parts.append(f"MAX({quote_ident(name_col)}) AS symbolname")
        else:
            select_parts.append("NULL AS symbolname")
        if dt_col:
            select_parts.append(f"MIN({quote_ident(dt_col)}) AS first_seen_at")
            select_parts.append(f"MAX({quote_ident(dt_col)}) AS last_seen_at")
        else:
            select_parts.append("NULL AS first_seen_at")
            select_parts.append("NULL AS last_seen_at")
        if rank_col:
            select_parts.append(f"MIN({quote_ident(rank_col)}) AS best_rank")
            select_parts.append(f"MAX({quote_ident(rank_col)}) AS last_rank")
        else:
            select_parts.append("NULL AS best_rank")
            select_parts.append("NULL AS last_rank")
        if price_col:
            select_parts.append(f"MAX({quote_ident(price_col)}) AS last_price")
        else:
            select_parts.append("NULL AS last_price")
        if volume_col:
            select_parts.append(f"MAX({quote_ident(volume_col)}) AS last_volume")
        else:
            select_parts.append("NULL AS last_volume")
        if ranking_type_col:
            select_parts.append(f"MAX({quote_ident(ranking_type_col)}) AS ranking_type")
        else:
            select_parts.append("NULL AS ranking_type")
        if market_col:
            select_parts.append(f"MAX({quote_ident(market_col)}) AS market")
        else:
            select_parts.append("NULL AS market")
        select_parts.append("COUNT(*) AS ranking_hit_count")

        where_parts = ["symbol IS NOT NULL", "TRIM(CAST(symbol AS TEXT)) <> ''"]
        params: list[Any] = []
        if dt_col:
            where_parts.append(f"substr(CAST({quote_ident(dt_col)} AS TEXT), 1, 10) = ?")
            params.append(td)
        if min_price is not None and price_col:
            where_parts.append(f"CAST({quote_ident(price_col)} AS REAL) >= ?")
            params.append(float(min_price))

        sql = f"""
            SELECT {', '.join(select_parts)}
              FROM {quote_ident(table)}
             WHERE {' AND '.join(where_parts)}
             GROUP BY symbol
             ORDER BY last_seen_at DESC
        """

        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        now = _now_str()
        out: list[dict[str, Any]] = []
        for r in rows:
            sym = _norm_symbol(r.get("symbol"))
            if not sym:
                continue
            out.append(
                {
                    "symbol": sym,
                    "trade_date": td,
                    "symbolname": r.get("symbolname"),
                    "datetime": r.get("last_seen_at") or r.get("first_seen_at") or now,
                    "rank": r.get("last_rank") or r.get("best_rank"),
                    "current_price": r.get("last_price"),
                    "trading_volume": r.get("last_volume"),
                    "ranking_type": r.get("ranking_type"),
                    "market": r.get("market"),
                    "source": table,
                    "active": 1 if active_only else 0,
                }
            )

        logger.info(
            "[YAHOO TRACKING CRUD] ranking symbols read db=%s table=%s trade_date=%s rows=%s min_price=%s",
            db,
            table,
            td,
            len(out),
            min_price,
        )
        return out
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def sync_tracking_symbols_from_ranking_db(
    *,
    ranking_db_path: str | Path,
    yahoo_db_path: str | Path | None = None,
    trade_date: Any = None,
    ranking_table: str = "ranking_snapshot_1min",
    min_price: float | None = 200.0,
) -> int:
    """Register today's ranking symbols into yahoo_tracking_state."""
    resolved_yahoo = str(yahoo_db_path or get_yahoo_1min_db_path(trade_date))
    ensure_tracking_state_db(db_path=resolved_yahoo, trade_date=trade_date)

    rows = read_today_ranking_symbols(
        ranking_db_path=ranking_db_path,
        trade_date=trade_date,
        table=ranking_table,
        min_price=min_price,
    )
    if not rows:
        return 0

    saved = upsert_yahoo_tracking_state_rows(
        rows,
        db_path=resolved_yahoo,
        trade_date=trade_date,
        source=ranking_table,
        increment_hit_count=True,
    )
    logger.info(
        "[YAHOO TRACKING CRUD] synced ranking->tracking ranking_db=%s yahoo_db=%s rows=%s",
        ranking_db_path,
        resolved_yahoo,
        saved,
    )
    return saved


def get_active_tracking_symbols(
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    resolved = ensure_tracking_state_db(db_path=db_path, trade_date=trade_date)
    td = normalize_trade_date(trade_date)
    sql = f"""
        SELECT *
          FROM {quote_ident(YAHOO_TRACKING_STATE_TABLE)}
         WHERE trade_date = ?
           AND COALESCE(active, 1) = 1
         ORDER BY COALESCE(last_seen_at, first_seen_at, updated_at) DESC, symbol ASC
    """
    params: list[Any] = [td]
    if limit is not None and int(limit) > 0:
        sql += " LIMIT ?"
        params.append(int(limit))

    con: sqlite3.Connection | None = None
    try:
        con = _connect(resolved)
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def get_tracking_state(
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
    symbol: str,
) -> Optional[dict[str, Any]]:
    resolved = ensure_tracking_state_db(db_path=db_path, trade_date=trade_date)
    td = normalize_trade_date(trade_date)
    sym = _norm_symbol(symbol)
    con: sqlite3.Connection | None = None
    try:
        con = _connect(resolved)
        row = con.execute(
            f"SELECT * FROM {quote_ident(YAHOO_TRACKING_STATE_TABLE)} WHERE symbol=? AND trade_date=? LIMIT 1",
            (sym, td),
        ).fetchone()
        return dict(row) if row else None
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def update_last_yahoo_downloaded_at(
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
    symbol: str,
    last_datetime: Any,
) -> int:
    resolved = ensure_tracking_state_db(db_path=db_path, trade_date=trade_date)
    td = normalize_trade_date(trade_date)
    sym = _norm_symbol(symbol)
    dt_s = _norm_dt(last_datetime)
    if not sym or not dt_s:
        return 0
    sql = f"""
        UPDATE {quote_ident(YAHOO_TRACKING_STATE_TABLE)}
           SET last_yahoo_downloaded_at = ?,
               last_yahoo_db_at = ?,
               updated_at = ?
         WHERE symbol = ?
           AND trade_date = ?
    """
    return _execute_write_with_retry(resolved, sql, (dt_s, dt_s, _now_str(), sym, td))


def update_last_summary_calculated_at(
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
    symbol: str,
    last_datetime: Any,
    interval: int = 1,
) -> int:
    resolved = ensure_tracking_state_db(db_path=db_path, trade_date=trade_date)
    td = normalize_trade_date(trade_date)
    sym = _norm_symbol(symbol)
    dt_s = _norm_dt(last_datetime)
    if not sym or not dt_s:
        return 0

    interval = int(interval)
    if interval == 1:
        col = "last_summary_calculated_at"
        db_col = "last_summary_1min_db_at"
    elif interval == 3:
        col = "last_3min_calculated_at"
        db_col = "last_summary_3min_db_at"
    elif interval == 5:
        col = "last_5min_calculated_at"
        db_col = "last_summary_5min_db_at"
    else:
        raise ValueError(f"unsupported interval: {interval}")

    sql = f"""
        UPDATE {quote_ident(YAHOO_TRACKING_STATE_TABLE)}
           SET {quote_ident(col)} = ?,
               {quote_ident(db_col)} = ?,
               updated_at = ?
         WHERE symbol = ?
           AND trade_date = ?
    """
    return _execute_write_with_retry(resolved, sql, (dt_s, dt_s, _now_str(), sym, td))


def mark_tracking_symbol_active(
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
    symbol: str,
    active: bool = True,
) -> int:
    resolved = ensure_tracking_state_db(db_path=db_path, trade_date=trade_date)
    td = normalize_trade_date(trade_date)
    sym = _norm_symbol(symbol)
    sql = f"""
        UPDATE {quote_ident(YAHOO_TRACKING_STATE_TABLE)}
           SET active = ?, updated_at = ?
         WHERE symbol = ?
           AND trade_date = ?
    """
    return _execute_write_with_retry(resolved, sql, (1 if active else 0, _now_str(), sym, td))


def _read_max_datetime_from_table(
    *,
    db_path: str | Path,
    table: str,
    symbol: str,
    trade_date: Any,
) -> Optional[str]:
    if not os.path.exists(str(db_path)):
        return None
    con: sqlite3.Connection | None = None
    try:
        con = _connect(db_path)
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        if row is None:
            return None
        cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()}
        if "symbol" not in cols or "datetime" not in cols:
            return None
        td = normalize_trade_date(trade_date)
        if "date" in cols:
            row2 = con.execute(
                f"SELECT MAX(datetime) FROM {quote_ident(table)} WHERE symbol=? AND date=?",
                (_norm_symbol(symbol), td),
            ).fetchone()
        else:
            row2 = con.execute(
                f"SELECT MAX(datetime) FROM {quote_ident(table)} WHERE symbol=? AND substr(CAST(datetime AS TEXT),1,10)=?",
                (_norm_symbol(symbol), td),
            ).fetchone()
        return row2[0] if row2 and row2[0] else None
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def repair_watermarks_from_existing_dbs(
    *,
    yahoo_db_path: str | Path | None = None,
    summary_db_path: str | Path | None = None,
    trade_date: Any = None,
    symbols: Iterable[str] | None = None,
) -> int:
    """
    If tracking_state is empty/stale but DB already has data, repair watermarks.

    This prevents re-download/re-calculation after restart.
    """
    resolved_yahoo = ensure_tracking_state_db(db_path=yahoo_db_path, trade_date=trade_date)
    td = normalize_trade_date(trade_date)

    if symbols is None:
        states = get_active_tracking_symbols(db_path=resolved_yahoo, trade_date=td)
        symbols = [r["symbol"] for r in states]

    updated = 0
    for sym in symbols:
        sym_s = _norm_symbol(sym)
        if not sym_s:
            continue

        yahoo_max = _read_max_datetime_from_table(
            db_path=resolved_yahoo,
            table=YAHOO_1MIN_TABLE,
            symbol=sym_s,
            trade_date=td,
        )
        if yahoo_max:
            updated += update_last_yahoo_downloaded_at(
                db_path=resolved_yahoo,
                trade_date=td,
                symbol=sym_s,
                last_datetime=yahoo_max,
            )

        if summary_db_path:
            for interval in (1, 3, 5):
                table = f"stock_summary_{interval}min"
                mx = _read_max_datetime_from_table(
                    db_path=summary_db_path,
                    table=table,
                    symbol=sym_s,
                    trade_date=td,
                )
                if mx:
                    updated += update_last_summary_calculated_at(
                        db_path=resolved_yahoo,
                        trade_date=td,
                        symbol=sym_s,
                        last_datetime=mx,
                        interval=interval,
                    )

    logger.info("[YAHOO TRACKING CRUD] repaired watermarks db=%s updated=%s", resolved_yahoo, updated)
    return updated


__all__ = [
    "ensure_tracking_state_db",
    "read_today_ranking_symbols",
    "sync_tracking_symbols_from_ranking_db",
    "get_active_tracking_symbols",
    "get_tracking_state",
    "update_last_yahoo_downloaded_at",
    "update_last_summary_calculated_at",
    "mark_tracking_symbol_active",
    "repair_watermarks_from_existing_dbs",
]
