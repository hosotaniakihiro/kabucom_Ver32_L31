# ============================================================
# File   : trading/yahoo/storage/yahoo_backfill_status.py
# Version: PRODUCTION-STABLE-REV1.1-YAHOO-BACKFILL-STATUS-DB-DATED
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完の取得状態を日付別DBで管理する。
#
# 【目的】
#   - 場中再起動後も「当日すでに取得済みの銘柄」を復元する
#   - yahoo_backfilled_symbols を DB 正本で管理する
#   - success / failed / pending を日付単位で追跡する
#   - global_data はキャッシュ、DB を正本とする
#
# 【保存先】
#   \\192.168.0.22\AutoStockBuyAndSell\raw_data\yahoo\status\
#       yahoo_backfill_status_YYYYMMDD.db
#
# 【主テーブル】
#   yahoo_backfill_status
#       symbol TEXT NOT NULL
#       trade_date TEXT NOT NULL
#       full_day_done INTEGER DEFAULT 0
#       status TEXT
#       last_downloaded_at TEXT
#       last_bar_datetime TEXT
#       rows INTEGER DEFAULT 0
#       error TEXT
#       updated_at TEXT
#       PRIMARY KEY(symbol, trade_date)
# ============================================================

from __future__ import annotations

import os
import sqlite3
import logging
import datetime as dt
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"


# ============================================================
# path helpers
# ============================================================

def _normalize_trade_date(trade_date: dt.date | str | None = None) -> str:
    if trade_date is None:
        return dt.date.today().strftime("%Y%m%d")

    if isinstance(trade_date, dt.date):
        return trade_date.strftime("%Y%m%d")

    s = str(trade_date).strip()
    if not s:
        return dt.date.today().strftime("%Y%m%d")

    return s.replace("-", "").replace("/", "")


def get_yahoo_status_db_path(
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> str:
    ymd = _normalize_trade_date(trade_date)
    root = os.path.join(base_dir, "raw_data", "yahoo", "status")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, f"yahoo_backfill_status_{ymd}.db")


def _normalize_symbol(symbol: object) -> str:
    if symbol is None:
        return ""

    s = str(symbol).strip()
    if not s:
        return ""

    s = s.replace(".T", "").replace(".JP", "").strip()
    if s.endswith(".0"):
        s = s[:-2]

    return s.strip()


def _normalize_symbols(symbols: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for s in symbols or []:
        ns = _normalize_symbol(s)
        if not ns or ns in seen:
            continue
        seen.add(ns)
        out.append(ns)

    return out


def _utc_now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_dt_str(value) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min).strftime("%Y-%m-%d %H:%M:%S")

    s = str(value).strip()
    return s or None


# ============================================================
# db bootstrap
# ============================================================

def ensure_yahoo_backfill_status_db(
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> str:
    db_path = get_yahoo_status_db_path(trade_date=trade_date, base_dir=base_dir)

    sql_table = """
    CREATE TABLE IF NOT EXISTS yahoo_backfill_status (
        symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        full_day_done INTEGER DEFAULT 0,
        status TEXT,
        last_downloaded_at TEXT,
        last_bar_datetime TEXT,
        rows INTEGER DEFAULT 0,
        error TEXT,
        updated_at TEXT,
        PRIMARY KEY(symbol, trade_date)
    )
    """

    sql_index1 = """
    CREATE INDEX IF NOT EXISTS idx_yahoo_backfill_status_trade_date
    ON yahoo_backfill_status(trade_date)
    """

    sql_index2 = """
    CREATE INDEX IF NOT EXISTS idx_yahoo_backfill_status_status
    ON yahoo_backfill_status(status)
    """

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(sql_table)
            conn.execute(sql_index1)
            conn.execute(sql_index2)
            conn.commit()

        logger.info("[YAHOO BACKFILL STATUS] ensured db=%s", db_path)
        return db_path

    except Exception:
        logger.exception("[YAHOO BACKFILL STATUS] ensure db failed path=%s", db_path)
        return db_path


# ============================================================
# upsert helpers
# ============================================================

def upsert_backfill_status(
    *,
    symbol: object,
    trade_date: dt.date | str | None = None,
    full_day_done: int = 0,
    status: str | None = None,
    last_downloaded_at=None,
    last_bar_datetime=None,
    rows: int | None = None,
    error: str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> bool:
    tdate = _normalize_trade_date(trade_date)
    db_path = ensure_yahoo_backfill_status_db(trade_date=tdate, base_dir=base_dir)

    sym = _normalize_symbol(symbol)

    if not sym:
        return False

    sql = """
    INSERT INTO yahoo_backfill_status (
        symbol,
        trade_date,
        full_day_done,
        status,
        last_downloaded_at,
        last_bar_datetime,
        rows,
        error,
        updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(symbol, trade_date)
    DO UPDATE SET
        full_day_done      = excluded.full_day_done,
        status             = excluded.status,
        last_downloaded_at = excluded.last_downloaded_at,
        last_bar_datetime  = excluded.last_bar_datetime,
        rows               = excluded.rows,
        error              = excluded.error,
        updated_at         = excluded.updated_at
    """

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(sql, (
                sym,
                tdate,
                int(full_day_done or 0),
                status,
                _to_dt_str(last_downloaded_at),
                _to_dt_str(last_bar_datetime),
                int(rows or 0),
                error,
                _utc_now_str(),
            ))
            conn.commit()

        return True

    except Exception:
        logger.exception(
            "[YAHOO BACKFILL STATUS] upsert failed symbol=%s trade_date=%s",
            sym,
            tdate,
        )
        return False


def mark_backfill_success(
    symbols: Iterable[object],
    *,
    trade_date: dt.date | str | None = None,
    rows_by_symbol: dict[str, int] | None = None,
    last_bar_by_symbol: dict[str, object] | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> int:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return 0

    done = 0
    tdate = _normalize_trade_date(trade_date)

    for sym in normalized:
        ok = upsert_backfill_status(
            symbol=sym,
            trade_date=tdate,
            full_day_done=1,
            status="success",
            last_downloaded_at=dt.datetime.now(),
            last_bar_datetime=(last_bar_by_symbol or {}).get(sym),
            rows=(rows_by_symbol or {}).get(sym, 0),
            error=None,
            base_dir=base_dir,
        )
        if ok:
            done += 1

    logger.info(
        "[YAHOO BACKFILL STATUS] marked success count=%s trade_date=%s",
        done,
        tdate,
    )
    return done


def mark_backfill_failed(
    symbols: Iterable[object],
    *,
    trade_date: dt.date | str | None = None,
    error: str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> int:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return 0

    done = 0
    tdate = _normalize_trade_date(trade_date)

    for sym in normalized:
        ok = upsert_backfill_status(
            symbol=sym,
            trade_date=tdate,
            full_day_done=0,
            status="failed",
            last_downloaded_at=dt.datetime.now(),
            last_bar_datetime=None,
            rows=0,
            error=error,
            base_dir=base_dir,
        )
        if ok:
            done += 1

    logger.warning(
        "[YAHOO BACKFILL STATUS] marked failed count=%s trade_date=%s",
        done,
        tdate,
    )
    return done


def mark_backfill_pending(
    symbols: Iterable[object],
    *,
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> int:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return 0

    done = 0
    tdate = _normalize_trade_date(trade_date)

    for sym in normalized:
        ok = upsert_backfill_status(
            symbol=sym,
            trade_date=tdate,
            full_day_done=0,
            status="pending",
            last_downloaded_at=None,
            last_bar_datetime=None,
            rows=0,
            error=None,
            base_dir=base_dir,
        )
        if ok:
            done += 1

    logger.info(
        "[YAHOO BACKFILL STATUS] marked pending count=%s trade_date=%s",
        done,
        tdate,
    )
    return done


# ============================================================
# readers
# ============================================================

def get_backfilled_symbols(
    *,
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> set[str]:
    tdate = _normalize_trade_date(trade_date)
    db_path = ensure_yahoo_backfill_status_db(trade_date=tdate, base_dir=base_dir)

    sql = """
    SELECT symbol
    FROM yahoo_backfill_status
    WHERE trade_date = ?
      AND full_day_done = 1
      AND status = 'success'
    """

    out: set[str] = set()

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            rows = conn.execute(sql, (tdate,)).fetchall()

        for row in rows:
            sym = _normalize_symbol(row[0] if row else None)
            if sym:
                out.add(sym)

        logger.info(
            "[YAHOO BACKFILL STATUS] loaded success symbols=%s trade_date=%s",
            len(out),
            tdate,
        )
        return out

    except Exception:
        logger.exception(
            "[YAHOO BACKFILL STATUS] load success symbols failed trade_date=%s",
            tdate,
        )
        return set()


def get_failed_symbols(
    *,
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> set[str]:
    tdate = _normalize_trade_date(trade_date)
    db_path = ensure_yahoo_backfill_status_db(trade_date=tdate, base_dir=base_dir)

    sql = """
    SELECT symbol
    FROM yahoo_backfill_status
    WHERE trade_date = ?
      AND status = 'failed'
    """

    out: set[str] = set()

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            rows = conn.execute(sql, (tdate,)).fetchall()

        for row in rows:
            sym = _normalize_symbol(row[0] if row else None)
            if sym:
                out.add(sym)

        return out

    except Exception:
        logger.exception(
            "[YAHOO BACKFILL STATUS] load failed symbols failed trade_date=%s",
            tdate,
        )
        return set()


def get_pending_symbols(
    *,
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> set[str]:
    tdate = _normalize_trade_date(trade_date)
    db_path = ensure_yahoo_backfill_status_db(trade_date=tdate, base_dir=base_dir)

    sql = """
    SELECT symbol
    FROM yahoo_backfill_status
    WHERE trade_date = ?
      AND status = 'pending'
      AND full_day_done = 0
    """

    out: set[str] = set()

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            rows = conn.execute(sql, (tdate,)).fetchall()

        for row in rows:
            sym = _normalize_symbol(row[0] if row else None)
            if sym:
                out.add(sym)

        return out

    except Exception:
        logger.exception(
            "[YAHOO BACKFILL STATUS] load pending symbols failed trade_date=%s",
            tdate,
        )
        return set()


def get_status_rows(
    *,
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> list[dict]:
    tdate = _normalize_trade_date(trade_date)
    db_path = ensure_yahoo_backfill_status_db(trade_date=tdate, base_dir=base_dir)

    sql = """
    SELECT
        symbol,
        trade_date,
        full_day_done,
        status,
        last_downloaded_at,
        last_bar_datetime,
        rows,
        error,
        updated_at
    FROM yahoo_backfill_status
    WHERE trade_date = ?
    ORDER BY symbol
    """

    out: list[dict] = []

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cur = conn.execute(sql, (tdate,))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()

        for row in rows:
            out.append(dict(zip(cols, row)))

        return out

    except Exception:
        logger.exception(
            "[YAHOO BACKFILL STATUS] load status rows failed trade_date=%s",
            tdate,
        )
        return []


# ============================================================
# sync helpers with runtime_symbols
# ============================================================

def restore_backfilled_symbols_to_runtime(
    *,
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> int:
    """
    DB上の success symbols を runtime_symbols / global_data へ戻す。
    """
    tdate = _normalize_trade_date(trade_date)
    symbols = get_backfilled_symbols(trade_date=tdate, base_dir=base_dir)
    if not symbols:
        return 0

    try:
        from trading.ranking.runtime_symbols import mark_yahoo_backfilled
        mark_yahoo_backfilled(symbols, target_date=tdate)

        logger.info(
            "[YAHOO BACKFILL STATUS] restored runtime backfilled symbols=%s trade_date=%s",
            len(symbols),
            tdate,
        )
        return len(symbols)

    except Exception:
        logger.exception(
            "[YAHOO BACKFILL STATUS] restore runtime backfilled failed trade_date=%s",
            tdate,
        )
        return 0


def _to_datetime_or_none(value) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min)
    try:
        import pandas as pd  # local import to keep this module lightweight
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.to_pydatetime().replace(second=0, microsecond=0)
    except Exception:
        return None


def get_status_map(
    *,
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> dict[str, dict]:
    """
    symbol -> status row の辞書を返す。
    Yahoo補完では「一度成功したら終了」ではなく、last_bar_datetime を見て
    20分遅れの到達時刻まで追いついているかを判定するために使う。
    """
    rows = get_status_rows(trade_date=trade_date, base_dir=base_dir)
    out: dict[str, dict] = {}
    for row in rows:
        sym = _normalize_symbol(row.get("symbol"))
        if sym:
            out[sym] = row
    return out


def compute_download_target_symbols(
    ranking_symbols: Iterable[object],
    *,
    trade_date: dt.date | str | None = None,
    target_end_dt=None,
    fresh_margin_minutes: int = 1,
    base_dir: str = DEFAULT_BASE_DIR,
) -> set[str]:
    """
    Yahoo取得対象を返す。

    旧仕様:
      当日ランキング銘柄 - DB上の success 銘柄
      → 一度成功すると、その後の20分遅れ更新対象から外れてしまう。

    新仕様:
      target_end_dt がある場合は、当日ランキング銘柄のうち
      last_bar_datetime < target_end_dt - fresh_margin_minutes の銘柄を返す。
      つまり、一度成功しても Yahoo の20分遅れ到達時刻まで未反映なら再取得する。

    target_end_dt がない場合だけ、後方互換として旧仕様を使う。
    """
    tdate = _normalize_trade_date(trade_date)
    ranking_set = set(_normalize_symbols(ranking_symbols))
    if not ranking_set:
        return set()

    target_end = _to_datetime_or_none(target_end_dt)
    if target_end is None:
        done_set = get_backfilled_symbols(trade_date=tdate, base_dir=base_dir)
        return ranking_set - done_set

    threshold = target_end - dt.timedelta(minutes=max(int(fresh_margin_minutes or 0), 0))
    status_map = get_status_map(trade_date=tdate, base_dir=base_dir)

    targets: set[str] = set()
    for sym in ranking_set:
        row = status_map.get(sym)
        if not row:
            targets.add(sym)
            continue

        last_bar = _to_datetime_or_none(row.get("last_bar_datetime"))
        if last_bar is None or last_bar < threshold:
            targets.add(sym)

    logger.info(
        "[YAHOO BACKFILL STATUS] incremental target ranking=%s targets=%s target_end=%s threshold=%s trade_date=%s",
        len(ranking_set),
        len(targets),
        target_end,
        threshold,
        tdate,
    )
    return targets


# ============================================================
# maintenance
# ============================================================

def delete_trade_date(
    *,
    trade_date: dt.date | str | None = None,
    base_dir: str = DEFAULT_BASE_DIR,
) -> int:
    tdate = _normalize_trade_date(trade_date)
    db_path = ensure_yahoo_backfill_status_db(trade_date=tdate, base_dir=base_dir)

    sql = "DELETE FROM yahoo_backfill_status WHERE trade_date = ?"

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cur = conn.execute(sql, (tdate,))
            conn.commit()
            count = cur.rowcount if cur.rowcount is not None else 0

        logger.warning(
            "[YAHOO BACKFILL STATUS] deleted rows=%s trade_date=%s",
            count,
            tdate,
        )
        return int(count)

    except Exception:
        logger.exception(
            "[YAHOO BACKFILL STATUS] delete trade_date failed trade_date=%s",
            tdate,
        )
        return 0


__all__ = [
    "DEFAULT_BASE_DIR",
    "get_yahoo_status_db_path",
    "ensure_yahoo_backfill_status_db",
    "upsert_backfill_status",
    "mark_backfill_success",
    "mark_backfill_failed",
    "mark_backfill_pending",
    "get_backfilled_symbols",
    "get_failed_symbols",
    "get_pending_symbols",
    "get_status_rows",
    "get_status_map",
    "restore_backfilled_symbols_to_runtime",
    "compute_download_target_symbols",
    "delete_trade_date",
]