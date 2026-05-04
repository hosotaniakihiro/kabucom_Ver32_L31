# ============================================================
# File   : database/upsert/yahoo_tracking_state_upsert.py
# Version: PRODUCTION-STABLE-REV1.0-YAHOO-TRACKING-STATE-UPSERT
# ------------------------------------------------------------
# Purpose:
#   yahoo_tracking_state UPSERT helpers.
#
# Notes:
#   - Used to register symbols that appeared in ranking_snapshot_1min.
#   - first_seen_at is preserved once set.
#   - ranking_hit_count is incremented when requested.
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from database.paths.yahoo_paths import get_yahoo_1min_db_path, normalize_trade_date
from database.schema.yahoo_tracking_state_schema import (
    YAHOO_TRACKING_STATE_TABLE,
    ensure_yahoo_schema,
)
from database.sqlite import DEFAULT_BUSY_TIMEOUT_MS, is_lock_error, lock_sleep_seconds, quote_ident

logger = logging.getLogger(__name__)

MAX_UPSERT_RETRY = 10


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
        # keep pandas optional and local
        import pandas as pd  # type: ignore

        x = pd.to_datetime(s, errors="coerce")
        if pd.isna(x):
            return s
        return x.to_pydatetime().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        import pandas as pd  # type: ignore

        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return None


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
    return con


def _prepare_row(row: Mapping[str, Any], *, trade_date: Any = None, source: str = "ranking_snapshot_1min") -> Optional[dict[str, Any]]:
    symbol = _norm_symbol(row.get("symbol"))
    if not symbol:
        return None

    now = _now_str()
    td = normalize_trade_date(row.get("trade_date") or row.get("date") or trade_date or now)

    seen_at = (
        _norm_dt(row.get("datetime"))
        or _norm_dt(row.get("snapshot_time"))
        or _norm_dt(row.get("inserted_at"))
        or now
    )

    price = _to_float(
        row.get("current_price")
        if row.get("current_price") is not None
        else row.get("close_price")
        if row.get("close_price") is not None
        else row.get("close")
    )

    volume = _to_float(
        row.get("trading_volume")
        if row.get("trading_volume") is not None
        else row.get("volume")
    )

    rank = _to_float(row.get("rank"))

    return {
        "symbol": symbol,
        "trade_date": td,
        "symbolname": row.get("symbolname") or row.get("name") or row.get("SymbolName"),
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
        "first_rank": rank,
        "best_rank": rank,
        "last_rank": rank,
        "last_price": price,
        "last_volume": volume,
        "ranking_type": row.get("ranking_type") or row.get("rank_type") or row.get("type"),
        "market": row.get("market") or row.get("exchange") or row.get("division"),
        "source": row.get("source") or source,
        "active": int(row.get("active", 1) if row.get("active", 1) is not None else 1),
        "created_at": now,
        "updated_at": now,
    }


def normalize_tracking_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    trade_date: Any = None,
    source: str = "ranking_snapshot_1min",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for row in rows or []:
        prepared = _prepare_row(row, trade_date=trade_date, source=source)
        if not prepared:
            continue
        key = (prepared["symbol"], prepared["trade_date"])
        if key in seen:
            # Same batch duplicate: keep last_seen/latest metadata, not duplicate SQL params.
            for old in out:
                if (old["symbol"], old["trade_date"]) == key:
                    old.update({k: v for k, v in prepared.items() if v is not None})
                    break
            continue
        seen.add(key)
        out.append(prepared)

    return out


def build_tracking_state_upsert_sql(*, increment_hit_count: bool = True) -> str:
    t = quote_ident(YAHOO_TRACKING_STATE_TABLE)
    cols = [
        "symbol",
        "trade_date",
        "symbolname",
        "first_seen_at",
        "last_seen_at",
        "first_rank",
        "best_rank",
        "last_rank",
        "last_price",
        "last_volume",
        "ranking_type",
        "market",
        "source",
        "active",
        "created_at",
        "updated_at",
    ]
    insert_cols = ", ".join(quote_ident(c) for c in cols)
    values = ", ".join(f":{c}" for c in cols)
    hit_expr = (
        "COALESCE(ranking_hit_count, 0) + 1"
        if increment_hit_count
        else "COALESCE(ranking_hit_count, 0)"
    )

    return f"""
        INSERT INTO {t} ({insert_cols})
        VALUES ({values})
        ON CONFLICT (symbol, trade_date) DO UPDATE SET
            symbolname = COALESCE(excluded.symbolname, {t}.symbolname),
            first_seen_at = COALESCE({t}.first_seen_at, excluded.first_seen_at),
            last_seen_at = COALESCE(excluded.last_seen_at, {t}.last_seen_at),
            first_rank = COALESCE({t}.first_rank, excluded.first_rank),
            best_rank = CASE
                WHEN {t}.best_rank IS NULL THEN excluded.best_rank
                WHEN excluded.best_rank IS NULL THEN {t}.best_rank
                WHEN excluded.best_rank < {t}.best_rank THEN excluded.best_rank
                ELSE {t}.best_rank
            END,
            last_rank = COALESCE(excluded.last_rank, {t}.last_rank),
            last_price = COALESCE(excluded.last_price, {t}.last_price),
            last_volume = COALESCE(excluded.last_volume, {t}.last_volume),
            ranking_type = COALESCE(excluded.ranking_type, {t}.ranking_type),
            market = COALESCE(excluded.market, {t}.market),
            source = COALESCE(excluded.source, {t}.source),
            active = COALESCE(excluded.active, {t}.active, 1),
            ranking_hit_count = {hit_expr},
            updated_at = excluded.updated_at
    """


def upsert_yahoo_tracking_state_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
    source: str = "ranking_snapshot_1min",
    increment_hit_count: bool = True,
) -> int:
    """Upsert rows into yahoo_tracking_state and return saved row count."""
    resolved = str(db_path or get_yahoo_1min_db_path(trade_date))
    ensure_yahoo_schema(resolved, trade_date=trade_date, ensure_1min=True, ensure_tracking=True)

    params = normalize_tracking_rows(rows, trade_date=trade_date, source=source)
    if not params:
        logger.info("[YAHOO TRACKING UPSERT] skipped empty rows db=%s", resolved)
        return 0

    sql = build_tracking_state_upsert_sql(increment_hit_count=increment_hit_count)
    last_err: Exception | None = None

    for attempt in range(1, MAX_UPSERT_RETRY + 1):
        con: sqlite3.Connection | None = None
        try:
            con = _connect(resolved)
            con.execute("BEGIN IMMEDIATE")
            con.executemany(sql, params)
            con.execute("COMMIT")
            logger.info("[YAHOO TRACKING UPSERT] ok db=%s rows=%s", resolved, len(params))
            return len(params)
        except Exception as e:
            last_err = e
            try:
                if con is not None:
                    con.execute("ROLLBACK")
            except Exception:
                pass
            if is_lock_error(e) and attempt < MAX_UPSERT_RETRY:
                sleep_s = lock_sleep_seconds(attempt, 0.35)
                logger.warning(
                    "[YAHOO TRACKING UPSERT] locked retry db=%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
                    resolved,
                    len(params),
                    attempt,
                    MAX_UPSERT_RETRY,
                    sleep_s,
                    str(e).splitlines()[0] if str(e) else type(e).__name__,
                )
                time.sleep(sleep_s)
                continue
            logger.exception("[YAHOO TRACKING UPSERT] failed db=%s rows=%s", resolved, len(params))
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


__all__ = [
    "normalize_tracking_rows",
    "build_tracking_state_upsert_sql",
    "upsert_yahoo_tracking_state_rows",
]
