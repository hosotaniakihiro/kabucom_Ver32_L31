# ============================================================
# File   : database/migrate/migrate_summary_sqlite.py
# Version: Ver36-STRUCTURED-SQLITE-SUMMARY-REBUILD-TMPNAME-FIX
# ------------------------------------------------------------
# ✔ Ver35 全機能保持（削除ゼロ）
# ✔ SQLite専用
# ✔ SAFE MIGRATION MODE 対応
# ✔ Base_summary create_all保持
# ✔ 列追加自己修復
# ✔ models.py 定義と完全整合
# ✔ 1min   -> UNIQUE(symbol, datetime)
# ✔ 3/5min -> UNIQUE(symbol, date, time_range)
# ✔ OHLC列名を open_price/high_price/low_price/close_price に統一
# ✔ 危険な1列CREATEを廃止
# ✔ sqlite_autoindex 残骸に対応
# ✔ 3min/5min は必要時テーブル再作成で legacy UNIQUE を除去
# ✔ 既存データは可能な限りコピー保持
# ✔ 重複整理後に正規UNIQUE再作成
# ✔ rebuild用一時テーブル名でも元テーブルのUNIQUE定義を参照
# ✔ KeyError: stock_summary_3min__rebuild_new 修正
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text

from database.bases import Base_summary

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
    # keys / identity
    "symbol": "TEXT",
    "symbolname": "TEXT",

    # datetime family
    "datetime": "DATETIME",
    "date": "TEXT",
    "time": "TEXT",
    "start_time": "TEXT",
    "end_time": "TEXT",
    "time_range": "TEXT",

    # OHLCV (models.py aligned)
    "open_price": "REAL",
    "high_price": "REAL",
    "low_price": "REAL",
    "close_price": "REAL",
    "volume": "REAL",

    # meta / scores
    "source": "TEXT",
    "score_buy": "REAL",
    "score_sell": "REAL",

    # slopes / indicators used by current pipeline
    "ma75_slope": "REAL",
    "volume_slope": "REAL",
    "vwap_slope": "REAL",
    "slope_atr_scaled": "REAL",

    "vwap": "REAL",
    "ma5": "REAL",
    "ma25": "REAL",
    "ma75": "REAL",

    "ma5_conf": "REAL",
    "ma25_conf": "REAL",
    "ma75_conf": "REAL",

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

LEGACY_UNIQUE_KEY_CANDIDATES: Dict[str, List[Tuple[str, ...]]] = {
    "stock_summary_1min": [
        ("symbol", "date", "time_range"),
    ],
    "stock_summary_3min": [
        ("symbol", "datetime"),
    ],
    "stock_summary_5min": [
        ("symbol", "datetime"),
    ],
}

REBUILD_SUFFIXES = (
    "__rebuild_new",
    "__rebuild_old",
    "__legacy_old",
    "__tmp_new",
    "__tmp_old",
)


# ============================================================
# INTERNAL HELPERS
# ============================================================

def _quote_ident(name: str) -> str:
    s = "" if name is None else str(name)
    s = s.replace('"', '""')
    return f'"{s}"'


def _base_table_name(table: str) -> str:
    """
    rebuild / tmp 用の一時テーブル名から、元の論理テーブル名を復元する。

    examples:
        stock_summary_3min__rebuild_new -> stock_summary_3min
        stock_summary_3min__legacy_old  -> stock_summary_3min
        stock_summary_3min              -> stock_summary_3min

    この関数がないと、_build_create_table_sql("stock_summary_3min__rebuild_new")
    のような呼び出し時に UNIQUE_KEY_BY_TABLE 参照で KeyError になる。
    """
    s = "" if table is None else str(table)

    for suffix in REBUILD_SUFFIXES:
        if s.endswith(suffix):
            return s[: -len(suffix)]

    return s


def _resolve_unique_key(table: str, *, base_table: Optional[str] = None) -> Tuple[str, ...]:
    """
    一時テーブル名でも、元テーブルの UNIQUE 定義を安全に取得する。
    """
    logical_table = base_table or _base_table_name(table)

    key_cols = UNIQUE_KEY_BY_TABLE.get(logical_table)
    if not key_cols:
        raise KeyError(
            f"UNIQUE_KEY_BY_TABLE missing: table={table}, logical_table={logical_table}"
        )

    return key_cols


def _fetch_existing_tables(conn) -> List[str]:
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0] is not None]


def _fetch_existing_columns(conn, table: str) -> List[str]:
    rows = conn.execute(
        text(f"PRAGMA table_info({_quote_ident(table)})")
    ).fetchall()
    return [str(r[1]) for r in rows if len(r) > 1 and r[1] is not None]


def _fetch_index_list(conn, table: str):
    return conn.execute(
        text(f"PRAGMA index_list({_quote_ident(table)})")
    ).fetchall()


def _fetch_index_columns(conn, index_name: str) -> Tuple[str, ...]:
    rows = conn.execute(
        text(f"PRAGMA index_info({_quote_ident(index_name)})")
    ).fetchall()

    cols: List[str] = []
    for r in rows:
        if len(r) > 2 and r[2] is not None:
            cols.append(str(r[2]).strip())

    return tuple(cols)


def _ensure_busy_and_wal(conn) -> None:
    try:
        conn.execute(text("PRAGMA busy_timeout=30000"))
    except Exception:
        logger.exception("[MIGRATE] failed to set busy_timeout")

    try:
        conn.execute(text("PRAGMA journal_mode=WAL"))
    except Exception:
        logger.exception("[MIGRATE] failed to set journal_mode=WAL")


def _ensure_table_exists_via_metadata(engine, table: str) -> None:
    with engine.begin() as conn:
        tables = set(_fetch_existing_tables(conn))
        if table in tables:
            return

    logger.info("🆕 CREATE TABLE via Base_summary.metadata.create_all → %s", table)
    Base_summary.metadata.create_all(engine)


def _ensure_table_and_column(engine, table: str, column: str, col_type: str) -> None:
    with engine.begin() as conn:
        _ensure_busy_and_wal(conn)

        tables = set(_fetch_existing_tables(conn))
        if table not in tables:
            logger.info("🆕 table missing → create_all retry: %s", table)
            Base_summary.metadata.create_all(engine)

            tables = set(_fetch_existing_tables(conn))
            if table not in tables:
                raise RuntimeError(
                    f"[MIGRATE] table creation failed even after create_all: {table}"
                )

        cols = set(_fetch_existing_columns(conn, table))

        if column not in cols:
            logger.info("➕ %s.%s (%s)", table, column, col_type)
            conn.execute(
                text(
                    f"ALTER TABLE {_quote_ident(table)} "
                    f"ADD COLUMN {_quote_ident(column)} {col_type}"
                )
            )


def _drop_index_if_exists(conn, index_name: str) -> None:
    try:
        conn.execute(text(f"DROP INDEX IF EXISTS {_quote_ident(index_name)}"))
    except Exception:
        logger.exception("[MIGRATE] failed to drop index: %s", index_name)


def _find_indexes_containing_key_columns(
    conn,
    table: str,
    key_cols: Tuple[str, ...],
) -> List[Tuple[str, str]]:
    """
    returns [(index_name, origin), ...]
    """
    out: List[Tuple[str, str]] = []

    try:
        for row in _fetch_index_list(conn, table):
            if len(row) < 3:
                continue

            index_name = str(row[1])
            is_unique = int(row[2]) == 1
            origin = (
                str(row[3]).strip().lower()
                if len(row) > 3 and row[3] is not None
                else ""
            )

            if not is_unique:
                continue

            cols = _fetch_index_columns(conn, index_name)
            if tuple(cols) == tuple(key_cols):
                out.append((index_name, origin))

    except Exception:
        logger.exception("[MIGRATE] failed to inspect indexes: %s", table)

    return out


def _delete_duplicate_rows_by_key(conn, table: str, key_cols: Tuple[str, ...]) -> int:
    """
    UNIQUE 作成前に key 単位の重複を削除する。
    最新 rowid を残す。
    """
    group_by = ", ".join([_quote_ident(c) for c in key_cols])

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


def _table_needs_rebuild_for_legacy_autoindex(conn, table: str) -> bool:
    """
    3min / 5min に legacy UNIQUE(symbol, datetime) が sqlite_autoindex として残っていたら
    DROP INDEX では消せないため、テーブル再作成が必要。
    """
    legacy_keys = LEGACY_UNIQUE_KEY_CANDIDATES.get(table, [])
    if not legacy_keys:
        return False

    try:
        for row in _fetch_index_list(conn, table):
            if len(row) < 3:
                continue

            index_name = str(row[1])
            is_unique = int(row[2]) == 1
            origin = (
                str(row[3]).strip().lower()
                if len(row) > 3 and row[3] is not None
                else ""
            )

            if not is_unique:
                continue

            cols = _fetch_index_columns(conn, index_name)

            for legacy_key in legacy_keys:
                if tuple(cols) == tuple(legacy_key):
                    if index_name.startswith("sqlite_autoindex") or origin == "u":
                        logger.warning(
                            "[MIGRATE] rebuild required due to legacy auto unique: "
                            "table=%s index=%s cols=%s origin=%s",
                            table,
                            index_name,
                            cols,
                            origin,
                        )
                        return True

    except Exception:
        logger.exception("[MIGRATE] failed to detect rebuild necessity: %s", table)

    return False


def _drop_legacy_conflicting_unique_indexes(conn, table: str) -> None:
    legacy_keys = LEGACY_UNIQUE_KEY_CANDIDATES.get(table, [])
    if not legacy_keys:
        return

    try:
        for row in _fetch_index_list(conn, table):
            if len(row) < 3:
                continue

            index_name = str(row[1])
            is_unique = int(row[2]) == 1
            origin = (
                str(row[3]).strip().lower()
                if len(row) > 3 and row[3] is not None
                else ""
            )

            if not is_unique:
                continue

            cols = _fetch_index_columns(conn, index_name)

            for legacy_key in legacy_keys:
                if tuple(cols) == tuple(legacy_key):
                    if index_name.startswith("sqlite_autoindex") or origin == "pk":
                        logger.warning(
                            "[MIGRATE] legacy conflicting autoindex remains and requires rebuild: "
                            "table=%s index=%s cols=%s",
                            table,
                            index_name,
                            cols,
                        )
                    else:
                        logger.warning(
                            "🧹 drop legacy conflicting UNIQUE index: table=%s index=%s cols=%s",
                            table,
                            index_name,
                            cols,
                        )
                        _drop_index_if_exists(conn, index_name)

    except Exception:
        logger.exception("[MIGRATE] failed to drop legacy conflicting indexes: %s", table)


def _build_create_table_sql(table: str, *, base_table: Optional[str] = None) -> str:
    """
    再作成用の最小完全テーブルDDL。
    models.py 整合の key / meta / current pipeline 重要列を持たせる。

    IMPORTANT:
      - table には stock_summary_3min__rebuild_new のような一時テーブル名が来る場合がある。
      - UNIQUE 定義は base_table / _base_table_name(table) から元テーブルを解決して使う。
    """
    key_cols = _resolve_unique_key(table, base_table=base_table)
    unique_sql = ", ".join([_quote_ident(c) for c in key_cols])

    col_defs = [
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "symbol TEXT NOT NULL",
        "symbolname TEXT",
        "datetime DATETIME NOT NULL",
        "date TEXT NOT NULL",
        "time TEXT",
        "start_time TEXT",
        "end_time TEXT",
        "time_range TEXT NOT NULL",
        "source TEXT",
        "open_price REAL",
        "high_price REAL",
        "low_price REAL",
        "close_price REAL",
        "volume REAL",
        "vwap REAL",
        "ma5 REAL",
        "ma25 REAL",
        "ma75 REAL",
        "ma5_conf REAL",
        "ma25_conf REAL",
        "ma75_conf REAL",
        "ma75_slope REAL",
        "volume_slope REAL",
        "vwap_slope REAL",
        "slope_atr_scaled REAL",
        "ema12 REAL",
        "ema26 REAL",
        "macd REAL",
        "signal REAL",
        "hist REAL",
        "rsi REAL",
        "rci REAL",
        "atr REAL",
        "bb_mid REAL",
        "bb_upper REAL",
        "bb_lower REAL",
        "bb_width REAL",
        "score_buy REAL",
        "score_sell REAL",
        "last_update DATETIME",
        f"UNIQUE({unique_sql})",
    ]

    cols_sql = ",\n            ".join(col_defs)

    return f"""
    CREATE TABLE {_quote_ident(table)} (
            {cols_sql}
    )
    """


def _copy_table_data(
    conn,
    src_table: str,
    dst_table: str,
    key_cols: Tuple[str, ...],
) -> None:
    src_cols = set(_fetch_existing_columns(conn, src_table))
    dst_cols = _fetch_existing_columns(conn, dst_table)

    copy_cols = [c for c in dst_cols if c != "id" and c in src_cols]
    if not copy_cols:
        logger.warning("[MIGRATE] no copyable columns found: %s -> %s", src_table, dst_table)
        return

    cols_sql = ", ".join([_quote_ident(c) for c in copy_cols])
    partition_sql = ", ".join([_quote_ident(c) for c in key_cols])

    # key単位で最新rowidを残してコピー
    sql = f"""
    INSERT INTO {_quote_ident(dst_table)} ({cols_sql})
    SELECT {cols_sql}
    FROM {_quote_ident(src_table)}
    WHERE rowid IN (
        SELECT MAX(rowid)
        FROM {_quote_ident(src_table)}
        GROUP BY {partition_sql}
    )
    """

    conn.execute(text(sql))


def _rebuild_table(engine, table: str) -> None:
    """
    sqlite_autoindex 由来の誤UNIQUEを除去するための再作成。

    修正点:
      - tmp_new に stock_summary_3min__rebuild_new のような一時名を使う。
      - ただし UNIQUE 定義は base_table=table として元テーブル定義を使う。
      - これにより KeyError: stock_summary_3min__rebuild_new を防止する。
    """
    tmp_old = f"{table}__legacy_old"
    tmp_new = f"{table}__rebuild_new"
    key_cols = _resolve_unique_key(table)

    logger.warning("🔁 rebuilding table to remove legacy autoindex UNIQUE: %s", table)

    with engine.begin() as conn:
        _ensure_busy_and_wal(conn)

        existing_tables = set(_fetch_existing_tables(conn))
        if table not in existing_tables:
            logger.info("[MIGRATE] rebuild skipped because table does not exist: %s", table)
            return

        conn.execute(text(f"DROP TABLE IF EXISTS {_quote_ident(tmp_old)}"))
        conn.execute(text(f"DROP TABLE IF EXISTS {_quote_ident(tmp_new)}"))

        # 現行テーブルを退避
        conn.execute(
            text(
                f"ALTER TABLE {_quote_ident(table)} RENAME TO {_quote_ident(tmp_old)}"
            )
        )

        try:
            # 正規DDLで新規作成
            # tmp_new は一時テーブル名だが、UNIQUE定義は元テーブル table を使う。
            conn.execute(text(_build_create_table_sql(tmp_new, base_table=table)))

            # データ移送（key単位で最新 rowid を採用）
            _copy_table_data(conn, tmp_old, tmp_new, key_cols)

            # 本番名へ差し替え
            conn.execute(text(f"DROP TABLE {_quote_ident(tmp_old)}"))
            conn.execute(
                text(
                    f"ALTER TABLE {_quote_ident(tmp_new)} RENAME TO {_quote_ident(table)}"
                )
            )

            logger.info("✅ rebuild complete: %s", table)

        except Exception:
            logger.exception("[MIGRATE] rebuild failed: %s", table)

            # 失敗時は可能な限り元へ戻す
            existing_tables_now = set(_fetch_existing_tables(conn))

            if tmp_new in existing_tables_now:
                conn.execute(text(f"DROP TABLE IF EXISTS {_quote_ident(tmp_new)}"))

            existing_tables_now = set(_fetch_existing_tables(conn))
            if table not in existing_tables_now and tmp_old in existing_tables_now:
                conn.execute(
                    text(
                        f"ALTER TABLE {_quote_ident(tmp_old)} RENAME TO {_quote_ident(table)}"
                    )
                )

            raise


def _ensure_unique_index(engine, table: str, key_cols: Tuple[str, ...]) -> None:
    index_name = f"uq_{table}_{'_'.join(key_cols)}"

    # sqlite_autoindex 残骸は先に再作成で除去
    with engine.begin() as conn:
        _ensure_busy_and_wal(conn)
        needs_rebuild = _table_needs_rebuild_for_legacy_autoindex(conn, table)

    if needs_rebuild:
        _rebuild_table(engine, table)

    with engine.begin() as conn:
        _ensure_busy_and_wal(conn)

        existing_columns = set(_fetch_existing_columns(conn, table))
        missing = [c for c in key_cols if c not in existing_columns]
        if missing:
            raise RuntimeError(
                f"[MIGRATE] cannot create unique index for {table}. missing columns={missing}"
            )

        _drop_legacy_conflicting_unique_indexes(conn, table)

        deleted = _delete_duplicate_rows_by_key(conn, table, key_cols)
        if deleted > 0:
            logger.warning(
                "🧹 duplicate rows removed before UNIQUE create: table=%s key=%s deleted=%s",
                table,
                key_cols,
                deleted,
            )

        matched_indexes = _find_indexes_containing_key_columns(conn, table, key_cols)
        if matched_indexes:
            logger.info("🔐 existing UNIQUE already matches: %s -> %s", table, matched_indexes)
            return

        _drop_index_if_exists(conn, index_name)

        cols_sql = ", ".join([_quote_ident(c) for c in key_cols])
        logger.info("🔐 UNIQUE追加 %s(%s)", table, ", ".join(key_cols))
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_quote_ident(index_name)} "
                f"ON {_quote_ident(table)}({cols_sql})"
            )
        )


def _migrate_one_table(engine, table: str) -> None:
    logger.info("🚧 migrate start: %s", table)

    _ensure_table_exists_via_metadata(engine, table)

    for col, typ in COMMON_REQUIRED_COLUMNS.items():
        _ensure_table_and_column(engine, table, col, typ)

    key_cols = _resolve_unique_key(table)
    _ensure_unique_index(engine, table, key_cols)

    logger.info("✅ migrate done: %s", table)


# ============================================================
# MAIN ENTRY
# ============================================================

def migrate_summary_sqlite(engine) -> None:
    """
    SQLite summary migration
    SAFE MIGRATION MODE 対応

    方針:
      - Base_summary の create_all を正とする
      - 危険な1列CREATEはしない
      - 列不足は ADD ONLY で補完
      - ただし sqlite_autoindex 由来の legacy UNIQUE は再作成で除去
      - UNIQUE は interval別に適用
          1min   -> (symbol, datetime)
          3/5min -> (symbol, date, time_range)
      - 列名は models.py と揃える
          open_price / high_price / low_price / close_price
    """
    print("📦 SQLite summary migration start")

    Base_summary.metadata.create_all(engine)

    for tbl in SUMMARY_TABLES:
        _migrate_one_table(engine, tbl)

    print("📦 SQLite summary migration complete")