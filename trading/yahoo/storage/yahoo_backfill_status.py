# ============================================================
# File   : trading/yahoo/storage/yahoo_backfill_status.py
# Version: PRODUCTION-STABLE-REV1.2-YAHOO-BACKFILL-STATUS-LOCK-SAFE-BDAY
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
#
# 【REV1.2 修正】
#   ✔ sqlite database is locked 対策
#      - busy_timeout
#      - WAL
#      - retry
#      - locked時だけsleepして再試行
#   ✔ trade_date を営業日に正規化
#      - 土日祝日なら直近営業日へ寄せる
#      - 例: 20260503(日) -> 20260501(金)
#   ✔ ensure / read / write / delete 全体に lock-safe 接続を適用
# ============================================================

from __future__ import annotations

import os
import time
import sqlite3
import logging
import datetime as dt
from typing import Iterable, Optional, Callable, TypeVar

logger = logging.getLogger(__name__)

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"

SQLITE_TIMEOUT_SEC = 30.0
SQLITE_BUSY_TIMEOUT_MS = 30000
SQLITE_RETRY_MAX = 6
SQLITE_RETRY_BASE_SLEEP_SEC = 0.25

_T = TypeVar("_T")


# ============================================================
# lock helpers
# ============================================================

def _is_sqlite_locked_error(e: Exception) -> bool:
    s = str(e).lower()
    return (
        "database is locked" in s
        or "database table is locked" in s
        or "database schema is locked" in s
        or "locked" in s
    )


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SEC,
        isolation_level=None,
    )

    try:
        conn.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_MS)}")
    except Exception:
        pass

    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        # NAS上や既存接続状態によって失敗することがあるため落とさない
        pass

    try:
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass

    return conn


def _with_sqlite_retry(
    *,
    label: str,
    db_path: str,
    fn: Callable[[sqlite3.Connection], _T],
    retry_max: int = SQLITE_RETRY_MAX,
    base_sleep: float = SQLITE_RETRY_BASE_SLEEP_SEC,
) -> Optional[_T]:
    """
    SQLite処理を lock-safe に実行する。

    - database is locked の時だけ retry
    - それ以外の sqlite error は即ログして None
    - fn内で commit する想定でも、ここで最後にcommit保険をかける
    """
    last_err: Exception | None = None

    for attempt in range(1, int(retry_max) + 1):
        conn: sqlite3.Connection | None = None

        try:
            conn = _connect(db_path)

            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                # BEGINでlockedになることが多い
                if _is_sqlite_locked_error(e):
                    raise
                # 読み取り系などでBEGIN不要な場合の保険
                pass

            ret = fn(conn)

            try:
                conn.commit()
            except Exception:
                pass

            return ret

        except sqlite3.OperationalError as e:
            last_err = e

            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass

            if _is_sqlite_locked_error(e):
                sleep_sec = float(base_sleep) * attempt
                logger.warning(
                    "[YAHOO BACKFILL STATUS] sqlite locked retry label=%s attempt=%s/%s sleep=%.2fs db=%s err=%s",
                    label,
                    attempt,
                    retry_max,
                    sleep_sec,
                    db_path,
                    e,
                )
                time.sleep(sleep_sec)
                continue

            logger.exception(
                "[YAHOO BACKFILL STATUS] sqlite operational error label=%s db=%s",
                label,
                db_path,
            )
            return None

        except Exception as e:
            last_err = e

            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass

            logger.exception(
                "[YAHOO BACKFILL STATUS] sqlite operation failed label=%s db=%s",
                label,
                db_path,
            )
            return None

        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    logger.error(
        "[YAHOO BACKFILL STATUS] sqlite retry exhausted label=%s db=%s err=%s",
        label,
        db_path,
        last_err,
    )
    return None


# ============================================================
# date helpers
# ============================================================

def _parse_ymd_to_date(value: dt.date | str | None) -> Optional[dt.date]:
    if value is None:
        return None

    if isinstance(value, dt.datetime):
        return value.date()

    if isinstance(value, dt.date):
        return value

    s = str(value).strip()

    if not s:
        return None

    s = s.replace("-", "").replace("/", "")[:8]

    try:
        return dt.datetime.strptime(s, "%Y%m%d").date()
    except Exception:
        return None


def _to_business_trade_date(d: dt.date) -> dt.date:
    """
    trade_date を営業日に正規化する。
    土日祝日なら直近営業日へ寄せる。
    """
    try:
        from utils.business_day_utils import is_business_day, get_previous_business_day

        if is_business_day(d):
            return d

        return get_previous_business_day(d)

    except Exception:
        # business_day_utilsが使えない場合は土日だけ最低限補正
        try:
            if d.weekday() < 5:
                return d

            x = d
            while x.weekday() >= 5:
                x -= dt.timedelta(days=1)
            return x
        except Exception:
            return d


def _normalize_trade_date(
    trade_date: dt.date | str | None = None,
    *,
    normalize_to_business_day: bool = True,
) -> str:
    """
    trade_date を YYYYMMDD に正規化する。

    REV1.2:
      normalize_to_business_day=True の場合、
      土日祝日は直近営業日に寄せる。
    """
    d = _parse_ymd_to_date(trade_date)

    if d is None:
        try:
            from utils.business_day_utils import get_effective_trade_date_for_startup

            d = get_effective_trade_date_for_startup()
        except Exception:
            d = dt.date.today()

    if normalize_to_business_day:
        d2 = _to_business_trade_date(d)
        if d2 != d:
            logger.info(
                "[YAHOO BACKFILL STATUS] trade_date normalized to business day: %s -> %s",
                d.strftime("%Y%m%d"),
                d2.strftime("%Y%m%d"),
            )
        d = d2

    return d.strftime("%Y%m%d")


# ============================================================
# path helpers
# ============================================================

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
    tdate = _normalize_trade_date(trade_date)
    db_path = get_yahoo_status_db_path(trade_date=tdate, base_dir=base_dir)

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

    def _op(conn: sqlite3.Connection) -> bool:
        conn.execute(sql_table)
        conn.execute(sql_index1)
        conn.execute(sql_index2)
        return True

    ok = _with_sqlite_retry(
        label="ensure_db",
        db_path=db_path,
        fn=_op,
    )

    if ok:
        logger.info("[YAHOO BACKFILL STATUS] ensured db=%s", db_path)
    else:
        logger.error("[YAHOO BACKFILL STATUS] ensure db failed path=%s", db_path)

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

    params = (
        sym,
        tdate,
        int(full_day_done or 0),
        status,
        _to_dt_str(last_downloaded_at),
        _to_dt_str(last_bar_datetime),
        int(rows or 0),
        error,
        _utc_now_str(),
    )

    def _op(conn: sqlite3.Connection) -> bool:
        conn.execute(sql, params)
        return True

    ok = _with_sqlite_retry(
        label=f"upsert_status:{sym}:{tdate}",
        db_path=db_path,
        fn=_op,
    )

    if ok:
        return True

    logger.error(
        "[YAHOO BACKFILL STATUS] upsert failed after retry symbol=%s trade_date=%s",
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

    def _op(conn: sqlite3.Connection) -> list[tuple]:
        return conn.execute(sql, (tdate,)).fetchall()

    rows = _with_sqlite_retry(
        label=f"get_backfilled_symbols:{tdate}",
        db_path=db_path,
        fn=_op,
    )

    out: set[str] = set()

    for row in rows or []:
        sym = _normalize_symbol(row[0] if row else None)
        if sym:
            out.add(sym)

    logger.info(
        "[YAHOO BACKFILL STATUS] loaded success symbols=%s trade_date=%s",
        len(out),
        tdate,
    )
    return out


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

    def _op(conn: sqlite3.Connection) -> list[tuple]:
        return conn.execute(sql, (tdate,)).fetchall()

    rows = _with_sqlite_retry(
        label=f"get_failed_symbols:{tdate}",
        db_path=db_path,
        fn=_op,
    )

    out: set[str] = set()

    for row in rows or []:
        sym = _normalize_symbol(row[0] if row else None)
        if sym:
            out.add(sym)

    return out


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

    def _op(conn: sqlite3.Connection) -> list[tuple]:
        return conn.execute(sql, (tdate,)).fetchall()

    rows = _with_sqlite_retry(
        label=f"get_pending_symbols:{tdate}",
        db_path=db_path,
        fn=_op,
    )

    out: set[str] = set()

    for row in rows or []:
        sym = _normalize_symbol(row[0] if row else None)
        if sym:
            out.add(sym)

    return out


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

    def _op(conn: sqlite3.Connection) -> tuple[list[str], list[tuple]]:
        cur = conn.execute(sql, (tdate,))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows

    ret = _with_sqlite_retry(
        label=f"get_status_rows:{tdate}",
        db_path=db_path,
        fn=_op,
    )

    out: list[dict] = []

    if not ret:
        return out

    cols, rows = ret

    for row in rows:
        out.append(dict(zip(cols, row)))

    return out


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
        import pandas as pd

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

    target_end_dt がある場合:
      last_bar_datetime < target_end_dt - fresh_margin_minutes の銘柄を返す。

    target_end_dt がない場合:
      後方互換として success済み銘柄を除外する。
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

    def _op(conn: sqlite3.Connection) -> int:
        cur = conn.execute(sql, (tdate,))
        return int(cur.rowcount if cur.rowcount is not None else 0)

    count = _with_sqlite_retry(
        label=f"delete_trade_date:{tdate}",
        db_path=db_path,
        fn=_op,
    )

    count = int(count or 0)

    logger.warning(
        "[YAHOO BACKFILL STATUS] deleted rows=%s trade_date=%s",
        count,
        tdate,
    )

    return count


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