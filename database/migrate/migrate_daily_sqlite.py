# ============================================================
# File   : database/migrate/migrate_daily_sqlite.py
# Version: Ver3.1-DAILY-SQLITE-SUMMARY-MODELS-ALIGNED-LOCK-SAFE-FINAL
# ------------------------------------------------------------
# ✔ Ver3.0 完全互換
# ✔ 旧 daily migration の危険な datetime UNIQUE 追加を廃止
# ✔ 1min   -> UNIQUE(symbol, datetime)
# ✔ 3/5min -> UNIQUE(symbol, date, time_range)
# ✔ open/high/low/close の後付けを廃止
# ✔ open_price/high_price/low_price/close_price に統一
# ✔ 既存DBの軽微補修専用
# ✔ sqlite_autoindex 残骸検出ログ追加
# ✔ 当日DB除外で migrate_main との二重実行を防止
# ✔ 過去日次 summary DB 補修専用
# ✔ database is locked 対策強化
# ✔ BEGIN IMMEDIATE / retry / 短トランザクション化
# ✔ 本番用 / 安全版
# ============================================================

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from config.paths import get_path

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

SUMMARY_TABLES = [
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
]

COMMON_REQUIRED_COLUMNS: Dict[str, str] = {
    "symbol": "TEXT",
    "symbolname": "TEXT",
    "datetime": "DATETIME",
    "date": "TEXT",
    "time": "TEXT",
    "start_time": "TEXT",
    "end_time": "TEXT",
    "time_range": "TEXT",
    "open_price": "REAL",
    "high_price": "REAL",
    "low_price": "REAL",
    "close_price": "REAL",
    "volume": "REAL",
    "source": "TEXT",
    "score_buy": "REAL",
    "score_sell": "REAL",
    "ma75_slope": "REAL",
    "volume_slope": "REAL",
    "vwap_slope": "REAL",
    "slope_atr_scaled": "REAL",
    "vwap": "REAL",
    "ma5": "REAL",
    "ma25": "REAL",
    "ma75": "REAL",
    "ema12": "REAL",
    "ema26": "REAL",
    "macd": "REAL",
    "signal": "REAL",
    "hist": "REAL",
    "rsi": "REAL",
    "rci": "REAL",
    "atr": "REAL",
    "bb_mid": "REAL",
    "bb_upper": "REAL",
    "bb_lower": "REAL",
    "bb_width": "REAL",
    "last_update": "DATETIME",
}

UNIQUE_KEY_BY_TABLE: Dict[str, Tuple[str, ...]] = {
    "stock_summary_1min": ("symbol", "datetime"),
    "stock_summary_3min": ("symbol", "date", "time_range"),
    "stock_summary_5min": ("symbol", "date", "time_range"),
}

LEGACY_BAD_KEYS: Dict[str, List[Tuple[str, ...]]] = {
    "stock_summary_1min": [("symbol", "date", "time_range")],
    "stock_summary_3min": [("symbol", "datetime")],
    "stock_summary_5min": [("symbol", "datetime")],
}

BUSY_TIMEOUT_MS = 60000
LOCK_RETRY_MAX = 5


# ============================================================
# HELPERS
# ============================================================

def _quote_ident(name: str) -> str:
    s = "" if name is None else str(name)
    s = s.replace('"', '""')
    return f'"{s}"'


def _is_locked_error(exc: Exception) -> bool:
    try:
        return "database is locked" in str(exc).lower()
    except Exception:
        return False


def _retry_wait(attempt: int) -> float:
    return float(min(2 * attempt, 10))


def _db_paths_under_summary_dir() -> List[Path]:
    """
    過去日次 summary DB のみ返す。
    当日DBは migrate_main -> migrate_summary_sqlite の正本経路で
    すでに処理される前提のため、daily では除外する。
    """
    base = get_path("summary")
    if not base.exists():
        return []

    today_name = f"summary{date.today():%Y%m%d}.db"

    files: List[Path] = []
    for p in sorted(base.glob("summary*.db")):
        if p.name == today_name:
            logger.info(
                "⏭ [DAILY MIGRATE] skip current-day summary DB: %s",
                p.name,
            )
            continue
        files.append(p)

    return files


def _fetch_tables(conn) -> List[str]:
    rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    return [str(r[0]) for r in rows if r and r[0] is not None]


def _fetch_columns(conn, table: str) -> List[str]:
    rows = conn.execute(text(f"PRAGMA table_info({_quote_ident(table)})")).fetchall()
    return [str(r[1]) for r in rows if len(r) > 1 and r[1] is not None]


def _fetch_index_list(conn, table: str):
    return conn.execute(text(f"PRAGMA index_list({_quote_ident(table)})")).fetchall()


def _fetch_index_columns(conn, index_name: str) -> Tuple[str, ...]:
    rows = conn.execute(text(f"PRAGMA index_info({_quote_ident(index_name)})")).fetchall()
    cols: List[str] = []
    for r in rows:
        if len(r) > 2 and r[2] is not None:
            cols.append(str(r[2]).strip())
    return tuple(cols)


def _set_pragmas(conn) -> None:
    conn.execute(text(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}"))
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA synchronous=NORMAL"))


def _begin_immediate(conn) -> None:
    conn.execute(text("BEGIN IMMEDIATE"))


def _ensure_column(conn, table: str, column: str, col_type: str) -> None:
    cols = set(_fetch_columns(conn, table))
    if column not in cols:
        logger.info("➕ %s.%s (%s)", table, column, col_type)
        conn.execute(
            text(
                f"ALTER TABLE {_quote_ident(table)} "
                f"ADD COLUMN {_quote_ident(column)} {col_type}"
            )
        )


def _delete_duplicates_once(conn, table: str, key_cols: Tuple[str, ...]) -> int:
    group_by = ", ".join(_quote_ident(c) for c in key_cols)
    sql = f"""
    DELETE FROM {_quote_ident(table)}
    WHERE rowid NOT IN (
        SELECT MAX(rowid)
        FROM {_quote_ident(table)}
        GROUP BY {group_by}
    )
    """
    result = conn.execute(text(sql))
    try:
        return int(result.rowcount or 0)
    except Exception:
        return 0


def _delete_duplicates(conn, table: str, key_cols: Tuple[str, ...]) -> int:
    last_error: Exception | None = None

    for attempt in range(1, LOCK_RETRY_MAX + 1):
        try:
            _set_pragmas(conn)
            _begin_immediate(conn)
            deleted = _delete_duplicates_once(conn, table, key_cols)
            conn.commit()
            return deleted

        except OperationalError as exc:
            try:
                conn.rollback()
            except Exception:
                pass

            last_error = exc
            if _is_locked_error(exc):
                wait_sec = _retry_wait(attempt)
                logger.warning(
                    "[DAILY MIGRATE] duplicate delete locked: table=%s key=%s "
                    "attempt=%s/%s wait=%.1fs",
                    table, key_cols, attempt, LOCK_RETRY_MAX, wait_sec,
                )
                time.sleep(wait_sec)
                continue
            raise

        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            last_error = exc
            raise

    if last_error is not None:
        raise last_error
    return 0


def _matching_unique_indexes(conn, table: str, key_cols: Tuple[str, ...]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for row in _fetch_index_list(conn, table):
        if len(row) < 3:
            continue
        index_name = str(row[1])
        is_unique = int(row[2]) == 1
        origin = str(row[3]).strip().lower() if len(row) > 3 and row[3] is not None else ""
        if not is_unique:
            continue
        cols = _fetch_index_columns(conn, index_name)
        if tuple(cols) == tuple(key_cols):
            out.append((index_name, origin))
    return out


def _detect_legacy_bad_unique(conn, table: str) -> List[Tuple[str, Tuple[str, ...], str]]:
    found: List[Tuple[str, Tuple[str, ...], str]] = []
    bad_keys = LEGACY_BAD_KEYS.get(table, [])
    if not bad_keys:
        return found

    for row in _fetch_index_list(conn, table):
        if len(row) < 3:
            continue
        index_name = str(row[1])
        is_unique = int(row[2]) == 1
        origin = str(row[3]).strip().lower() if len(row) > 3 and row[3] is not None else ""
        if not is_unique:
            continue
        cols = _fetch_index_columns(conn, index_name)
        for bad in bad_keys:
            if tuple(cols) == tuple(bad):
                found.append((index_name, tuple(cols), origin))
    return found


def _drop_non_auto_legacy_indexes(conn, table: str) -> None:
    for index_name, cols, origin in _detect_legacy_bad_unique(conn, table):
        if index_name.startswith("sqlite_autoindex") or origin in ("pk", "u"):
            logger.warning(
                "[DAILY MIGRATE] legacy auto unique remains (rebuild required): "
                "table=%s index=%s cols=%s origin=%s",
                table, index_name, cols, origin,
            )
            continue

        logger.warning(
            "🧹 [DAILY MIGRATE] drop legacy unique: table=%s index=%s cols=%s",
            table, index_name, cols,
        )
        conn.execute(text(f"DROP INDEX IF EXISTS {_quote_ident(index_name)}"))


def _ensure_correct_unique_only(conn, table: str) -> None:
    key_cols = UNIQUE_KEY_BY_TABLE[table]

    _drop_non_auto_legacy_indexes(conn, table)

    deleted = _delete_duplicates(conn, table, key_cols)
    if deleted > 0:
        logger.warning(
            "🧹 [DAILY MIGRATE] duplicate rows removed: table=%s key=%s deleted=%s",
            table, key_cols, deleted
        )

    matched = _matching_unique_indexes(conn, table, key_cols)
    if matched:
        logger.info("🔐 existing UNIQUE already matches: %s -> %s", table, matched)
        return

    index_name = f"uq_{table}_{'_'.join(key_cols)}"
    cols_sql = ", ".join(_quote_ident(c) for c in key_cols)

    conn.execute(text("BEGIN IMMEDIATE"))
    try:
        logger.info("🔐 UNIQUE追加 %s(%s)", table, ", ".join(key_cols))
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_quote_ident(index_name)} "
                f"ON {_quote_ident(table)}({cols_sql})"
            )
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def _migrate_one_table(conn, db_path: Path, table: str) -> None:
    tables = set(_fetch_tables(conn))
    if table not in tables:
        logger.info(
            "⏭ [DAILY MIGRATE] table not found skip: db=%s table=%s",
            db_path.name,
            table,
        )
        return

    # 列補修は比較的軽いので短い transaction で実行
    conn.execute(text("BEGIN IMMEDIATE"))
    try:
        for col, typ in COMMON_REQUIRED_COLUMNS.items():
            _ensure_column(conn, table, col, typ)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    # 正しいUNIQUEのみ保証
    _ensure_correct_unique_only(conn, table)


def _migrate_one_db(db_path: Path) -> None:
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 60},
    )

    try:
        with engine.connect() as conn:
            _set_pragmas(conn)

            for table in SUMMARY_TABLES:
                _migrate_one_table(conn, db_path, table)

        logger.info("✔ migrated %s", db_path.name)

    finally:
        engine.dispose()


# ============================================================
# MAIN ENTRY
# ============================================================

def migrate_daily_sqlite() -> None:
    """
    過去日次 summary DB の軽微補修専用。

    重要:
      - 3/5分足へ UNIQUE(symbol, datetime) を追加しない
      - open/high/low/close を追加しない
      - 正本は migrate_summary_sqlite に譲る
      - sqlite_autoindex 由来の legacy UNIQUE はログで rebuild 必要を通知
      - 当日DBは migrate_main 側の正本 migration で処理するため除外
    """
    print("📦 daily SQLite summary migration start")

    db_files = _db_paths_under_summary_dir()
    if not db_files:
        print("📦 daily SQLite summary migration complete")
        return

    failed: List[str] = []

    for db_path in db_files:
        try:
            _migrate_one_db(db_path)
        except Exception:
            failed.append(str(db_path))
            logger.exception("[DAILY MIGRATE] failed: %s", db_path)

    if failed:
        logger.warning("[DAILY MIGRATE] failed db count=%s", len(failed))
        for p in failed:
            logger.warning("[DAILY MIGRATE] failed db path=%s", p)

    print("📦 daily SQLite summary migration complete")


__all__ = [
    "migrate_daily_sqlite",
]

