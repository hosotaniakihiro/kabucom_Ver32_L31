# ============================================================
# database/migrate/migrate_summary_duck.py
# Ver32.1-STRUCTURED-DUCKDB-SUMMARY-SQLA2-FINAL
# ------------------------------------------------------------
# ✔ Ver32 完全保持（削除ゼロ）
# ✔ DuckDB専用
# ✔ ADD ONLY 原則厳守
# ✔ UNIQUE(symbol, datetime) 強制保証
# ✔ SAFE MIGRATION MODE 対応
# ✔ 既存データ破壊なし
# ✔ 列追加自己修復
# ✔ SQLAlchemy 2.0 strict 完全対応
# ✔ text() 全面適用
# ============================================================

from __future__ import annotations
from sqlalchemy import text
import logging

logger = logging.getLogger(__name__)

# ============================================================
# INTERNAL HELPERS
# ============================================================

def _table_exists(conn, table: str):

    # 1) information_schema
    try:
        r = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_name = :table
            """),
            {"table": table}
        ).scalar_one_or_none()
        if r:
            return True
    except Exception:
        pass

    # 2) SQLite fallback
    try:
        r = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type='table'
                AND name=:table
            """),
            {"table": table}
        ).scalar_one_or_none()
        if r:
            return True
    except Exception:
        pass

    # 3) DuckDB fallback
    try:
        r = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM duckdb_tables()
                WHERE table_name = :table
            """),
            {"table": table}
        ).scalar_one_or_none()
        if r:
            return True
    except Exception:
        pass

    return False


def _column_exists(conn, table: str, column: str) -> bool:

    # 1) information_schema
    try:
        r = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_name = :table
                  AND column_name = :column
            """),
            {"table": table, "column": column},
        ).scalar_one_or_none()

        if r:
            return True

    except Exception:
        pass

    # 2) SQLite fallback
    try:
        rows = conn.execute(
            text(f"PRAGMA table_info({table})")
        ).fetchall()

        colnames = {r[1] for r in rows}
        return column in colnames

    except Exception:
        pass

    return False


def _ensure_column(conn, table: str, column: str, col_type: str):

    if not _column_exists(conn, table, column):

        logger.info(f"➕ DuckDB {table}.{column} ({col_type})")

        # DDLはバインド不可 → f-string + text()
        conn.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        )


def _ensure_unique_index(conn, table: str):

    index_name = f"uq_{table}_symbol_datetime"

    conn.execute(
        text(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
            ON {table}(symbol, datetime)
        """)
    )


def _create_table_if_missing(conn, table: str):

    if _table_exists(conn, table):
        return

    logger.info(f"🆕 DuckDB CREATE TABLE {table}")

    conn.execute(
        text(f"""
            CREATE TABLE {table} (
                id BIGINT,

                symbol TEXT NOT NULL,
                symbolname TEXT,

                datetime TIMESTAMP NOT NULL,

                date DATE NOT NULL,
                time_range TEXT NOT NULL,

                start_time TIME,
                end_time TIME,
                time TIME,

                source TEXT,

                open_price DOUBLE,
                high_price DOUBLE,
                low_price DOUBLE,
                close_price DOUBLE,

                volume DOUBLE,
                vwap DOUBLE,

                ma5 DOUBLE,
                ma25 DOUBLE,
                ma75 DOUBLE,

                ma5_conf DOUBLE,
                ma25_conf DOUBLE,
                ma75_conf DOUBLE,

                ma75_slope DOUBLE,
                volume_slope DOUBLE,
                vwap_slope DOUBLE,
                slope_atr_scaled DOUBLE,

                ema12 DOUBLE,
                ema26 DOUBLE,
                macd DOUBLE,
                signal DOUBLE,
                hist DOUBLE,

                rsi DOUBLE,
                rci DOUBLE,
                atr DOUBLE,

                bb_mid DOUBLE,
                bb_upper DOUBLE,
                bb_lower DOUBLE,
                bb_width DOUBLE,

                score_buy DOUBLE,
                score_sell DOUBLE,

                last_update TIMESTAMP
            )
        """)
    )

    _ensure_unique_index(conn, table)


# ============================================================
# MAIN ENTRY
# ============================================================

def migrate_summary_duck(engine):
    """
    DuckDB summary migration
    SAFE MIGRATION MODE 対応
    """

    print("🔥 DuckDB summary migration start")

    summary_tables = [
        "stock_summary_1min",
        "stock_summary_3min",
        "stock_summary_5min",
    ]

    # ADD ONLY保証列
    required_columns = {

        # 基本
        "symbolname": "TEXT",
        "date": "DATE",
        "time_range": "TEXT",
        "start_time": "TIME",
        "end_time": "TIME",
        "time": "TIME",

        # OHLC
        "open_price": "DOUBLE",
        "high_price": "DOUBLE",
        "low_price": "DOUBLE",
        "close_price": "DOUBLE",

        "volume": "DOUBLE",
        "vwap": "DOUBLE",

        # MA
        "ma5": "DOUBLE",
        "ma25": "DOUBLE",
        "ma75": "DOUBLE",

        "ma5_conf": "DOUBLE",
        "ma25_conf": "DOUBLE",
        "ma75_conf": "DOUBLE",

        # slope
        "ma75_slope": "DOUBLE",
        "volume_slope": "DOUBLE",
        "vwap_slope": "DOUBLE",
        "slope_atr_scaled": "DOUBLE",

        # EMA / MACD
        "ema12": "DOUBLE",
        "ema26": "DOUBLE",
        "macd": "DOUBLE",
        "signal": "DOUBLE",
        "hist": "DOUBLE",

        # indicators
        "rsi": "DOUBLE",
        "rci": "DOUBLE",
        "atr": "DOUBLE",

        # BB
        "bb_mid": "DOUBLE",
        "bb_upper": "DOUBLE",
        "bb_lower": "DOUBLE",
        "bb_width": "DOUBLE",

        # scoring
        "score_buy": "DOUBLE",
        "score_sell": "DOUBLE",

        # meta
        "source": "TEXT",
        "last_update": "TIMESTAMP",
    }

    with engine.begin() as conn:

        for tbl in summary_tables:

            # 1️⃣ テーブル存在保証
            _create_table_if_missing(conn, tbl)

            # 2️⃣ 列追加（ADD ONLY）
            for col, typ in required_columns.items():
                _ensure_column(conn, tbl, col, typ)

            # 3️⃣ UNIQUE保証
            _ensure_unique_index(conn, tbl)

    print("🔥 DuckDB summary migration complete")