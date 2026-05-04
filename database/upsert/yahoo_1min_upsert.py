# ============================================================
# File   : database/upsert/yahoo_1min_upsert.py
# Version: PRODUCTION-STABLE-REV1.0-YAHOO-1MIN-UPSERT
# ------------------------------------------------------------
# Purpose:
#   Yahoo 1min OHLCV rows UPSERT helper.
#
# Guarantees:
#   - PRIMARY KEY(symbol, datetime) UPSERT
#   - date/time and OHLC aliases are normalized
#   - Existing PUSH summary is not touched here; this only saves yahoo_1min
#   - Lock retry for NAS SQLite
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from database.paths.yahoo_paths import get_yahoo_1min_db_path, normalize_trade_date
from database.schema.yahoo_tracking_state_schema import YAHOO_1MIN_TABLE, ensure_yahoo_schema
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


def _norm_datetime(value: Any) -> Optional[str]:
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
        try:
            if x.tzinfo is not None:
                try:
                    x = x.tz_convert(None)
                except Exception:
                    x = x.tz_localize(None)
        except Exception:
            pass
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


def _extract_datetime(row: Mapping[str, Any]) -> Optional[str]:
    for key in ("datetime", "Datetime", "timestamp", "Timestamp", "date_time", "time"):
        v = row.get(key)
        if v is not None:
            x = _norm_datetime(v)
            if x:
                return x
    return None


def _get_price(row: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key in row and row.get(key) is not None:
            return _to_float(row.get(key))
    return None


def _prepare_row(row: Mapping[str, Any], *, trade_date: Any = None, source: str = "yahoo") -> Optional[dict[str, Any]]:
    symbol = _norm_symbol(row.get("symbol") or row.get("Symbol"))
    dt_s = _extract_datetime(row)
    if not symbol or not dt_s:
        return None

    open_v = _get_price(row, "open", "Open", "open_price")
    high_v = _get_price(row, "high", "High", "high_price")
    low_v = _get_price(row, "low", "Low", "low_price")
    close_v = _get_price(row, "close", "Close", "close_price", "current_price")
    vol_v = _get_price(row, "volume", "Volume", "trading_volume")

    # If yfinance returns Close only for a partial row, keep OHLC compatible.
    if close_v is not None:
        open_v = close_v if open_v is None else open_v
        high_v = close_v if high_v is None else high_v
        low_v = close_v if low_v is None else low_v

    try:
        d = dt_s[0:10]
        t = dt_s[11:19]
    except Exception:
        d = normalize_trade_date(trade_date)
        t = None

    return {
        "symbol": symbol,
        "datetime": dt_s,
        "date": d,
        "time": t,
        "open": open_v,
        "high": high_v,
        "low": low_v,
        "close": close_v,
        "open_price": open_v,
        "high_price": high_v,
        "low_price": low_v,
        "close_price": close_v,
        "volume": vol_v if vol_v is not None else 0.0,
        "source": row.get("source") or source,
        "updated_at": _now_str(),
    }


def normalize_yahoo_1min_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    trade_date: Any = None,
    source: str = "yahoo",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for row in rows or []:
        prepared = _prepare_row(row, trade_date=trade_date, source=source)
        if not prepared:
            continue
        key = (prepared["symbol"], prepared["datetime"])
        if key in seen:
            for old in out:
                if (old["symbol"], old["datetime"]) == key:
                    old.update({k: v for k, v in prepared.items() if v is not None})
                    break
            continue
        seen.add(key)
        out.append(prepared)

    out.sort(key=lambda x: (x["symbol"], x["datetime"]))
    return out


def dataframe_to_yahoo_rows(df: Any, *, symbol: str | None = None) -> list[dict[str, Any]]:
    """Convert yfinance/pandas DataFrame into normalized row dicts."""
    if df is None:
        return []

    try:
        import pandas as pd  # type: ignore

        if not isinstance(df, pd.DataFrame) or df.empty:
            return []

        work = df.copy()
        if "datetime" not in work.columns:
            idx_name = work.index.name or "datetime"
            work = work.reset_index().rename(columns={idx_name: "datetime"})

        # Flatten MultiIndex columns if needed.
        if getattr(work.columns, "nlevels", 1) > 1:
            work.columns = ["_".join(str(x) for x in c if str(x) != "") for c in work.columns]

        if symbol and "symbol" not in work.columns:
            work["symbol"] = symbol

        return work.to_dict("records")
    except Exception:
        logger.exception("[YAHOO 1MIN UPSERT] dataframe conversion failed")
        return []


def build_yahoo_1min_upsert_sql() -> str:
    t = quote_ident(YAHOO_1MIN_TABLE)
    cols = [
        "symbol",
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "source",
        "updated_at",
    ]
    insert_cols = ", ".join(quote_ident(c) for c in cols)
    values = ", ".join(f":{c}" for c in cols)
    updates = ",\n            ".join(
        f"{quote_ident(c)} = excluded.{quote_ident(c)}"
        for c in cols
        if c not in {"symbol", "datetime"}
    )
    return f"""
        INSERT INTO {t} ({insert_cols})
        VALUES ({values})
        ON CONFLICT (symbol, datetime) DO UPDATE SET
            {updates}
    """


def upsert_yahoo_1min_rows(
    rows: Sequence[Mapping[str, Any]] | Any,
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
    source: str = "yahoo",
    symbol: str | None = None,
) -> int:
    """Upsert Yahoo 1min rows. Accepts list[dict] or pandas DataFrame."""
    resolved = str(db_path or get_yahoo_1min_db_path(trade_date))
    ensure_yahoo_schema(resolved, trade_date=trade_date, ensure_1min=True, ensure_tracking=True)

    if not isinstance(rows, (list, tuple)):
        rows = dataframe_to_yahoo_rows(rows, symbol=symbol)

    params = normalize_yahoo_1min_rows(rows, trade_date=trade_date, source=source)
    if not params:
        logger.info("[YAHOO 1MIN UPSERT] skipped empty rows db=%s", resolved)
        return 0

    sql = build_yahoo_1min_upsert_sql()
    last_err: Exception | None = None

    for attempt in range(1, MAX_UPSERT_RETRY + 1):
        con: sqlite3.Connection | None = None
        try:
            con = _connect(resolved)
            con.execute("BEGIN IMMEDIATE")
            con.executemany(sql, params)
            con.execute("COMMIT")
            logger.info("[YAHOO 1MIN UPSERT] ok db=%s rows=%s", resolved, len(params))
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
                    "[YAHOO 1MIN UPSERT] locked retry db=%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
                    resolved,
                    len(params),
                    attempt,
                    MAX_UPSERT_RETRY,
                    sleep_s,
                    str(e).splitlines()[0] if str(e) else type(e).__name__,
                )
                time.sleep(sleep_s)
                continue
            logger.exception("[YAHOO 1MIN UPSERT] failed db=%s rows=%s", resolved, len(params))
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


def get_yahoo_1min_latest_datetime(
    *,
    db_path: str | Path | None = None,
    trade_date: Any = None,
    symbol: str,
) -> Optional[str]:
    resolved = str(db_path or get_yahoo_1min_db_path(trade_date))
    ensure_yahoo_schema(resolved, trade_date=trade_date, ensure_1min=True, ensure_tracking=True)
    sym = _norm_symbol(symbol)
    td = normalize_trade_date(trade_date) if trade_date is not None else None

    con: sqlite3.Connection | None = None
    try:
        con = _connect(resolved)
        if td:
            row = con.execute(
                f"SELECT MAX(datetime) FROM {quote_ident(YAHOO_1MIN_TABLE)} WHERE symbol=? AND date=?",
                (sym, td),
            ).fetchone()
        else:
            row = con.execute(
                f"SELECT MAX(datetime) FROM {quote_ident(YAHOO_1MIN_TABLE)} WHERE symbol=?",
                (sym,),
            ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


__all__ = [
    "normalize_yahoo_1min_rows",
    "dataframe_to_yahoo_rows",
    "build_yahoo_1min_upsert_sql",
    "upsert_yahoo_1min_rows",
    "get_yahoo_1min_latest_datetime",
]
