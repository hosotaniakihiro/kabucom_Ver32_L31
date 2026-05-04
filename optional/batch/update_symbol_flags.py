# ============================================================
# File   : optional/batch/update_symbol_flags.py
# Version: Ver2.1-PRODUCTION-SYMBOL-FLAGS-TARGET-UPDATER-PATH-SAFE-FINAL
# ------------------------------------------------------------
# ✔ symbol_flags.db の symbol_flags を直接更新
# ✔ market_type が プライム / スタンダード / グロース のみ対象
# ✔ buy_target は通常株のみ 1
# ✔ sell_target は 貸借銘柄 のみ 1
# ✔ ETF / ETN / REIT / FUND / 指数連動系を除外
# ✔ 既存列差異に安全対応
# ✔ dry-run 対応
# ✔ 更新前後件数ログ出力
# ✔ backup file 作成対応
# ✔ ingest_all_optional_data.py からの互換呼び出し対応
# ✔ DB誤指定時に対象DBまで明示
# ✔ SyntaxError 修正済み
# ✔ production hardened
# ============================================================

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
DEFAULT_DB_PATH = r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db"
TABLE_NAME = "symbol_flags"

ALLOWED_MARKETS = ("プライム", "スタンダード", "グロース")

EXCLUDE_KEYWORDS = (
    "ETF",
    "ETN",
    "REIT",
    "FUND",
    "J-REIT",
    "インデックス",
    "指数",
    "連動",
    "レバ",
    "ブル",
    "ベア",
    "インバース",
)

# ------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# ============================================================
# SQLITE HELPERS
# ============================================================
def connect_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name=?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sql_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def fetch_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table_name)})").fetchall()
    cols: set[str] = set()

    for r in rows:
        try:
            name = str(r["name"]).strip()
        except Exception:
            name = ""
        if name:
            cols.add(name)

    return cols


def ensure_required_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    cols = fetch_table_columns(conn, table_name)

    required = {
        "symbol",
        "symbolname",
        "market_type",
        "buy_target",
        "sell_target",
        "credit_type",
    }
    missing = [c for c in required if c not in cols]
    if missing:
        raise RuntimeError(f"required columns missing in {table_name}: {missing}")

    return cols


# ============================================================
# BACKUP
# ============================================================
def backup_db_file(db_path: str) -> str:
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"DB file not found: {db_path}")

    backup_path = src.with_suffix(src.suffix + ".bak")
    shutil.copy2(src, backup_path)
    logger.info("backup created: %s", backup_path)
    return str(backup_path)


# ============================================================
# SQL BUILD
# ============================================================
def build_market_list_sql() -> str:
    return ", ".join(sql_quote(m) for m in ALLOWED_MARKETS)


def build_exclude_name_sql(column_name: str = "symbolname") -> str:
    parts: list[str] = []
    for kw in EXCLUDE_KEYWORDS:
        safe_kw = str(kw).replace("'", "''")
        parts.append(f"COALESCE({column_name}, '') NOT LIKE '%{safe_kw}%'")
    return " AND ".join(parts)


def build_reset_sql(table_name: str) -> str:
    return f"""
    UPDATE {quote_ident(table_name)}
    SET
        buy_target = 0,
        sell_target = 0
    """


def build_buy_update_sql(table_name: str) -> str:
    market_list = build_market_list_sql()
    exclude_cond = build_exclude_name_sql("symbolname")

    return f"""
    UPDATE {quote_ident(table_name)}
    SET buy_target = 1
    WHERE market_type IN ({market_list})
      AND {exclude_cond}
    """


def build_sell_update_sql(table_name: str) -> str:
    market_list = build_market_list_sql()
    exclude_cond = build_exclude_name_sql("symbolname")

    return f"""
    UPDATE {quote_ident(table_name)}
    SET sell_target = 1
    WHERE market_type IN ({market_list})
      AND credit_type = '貸借銘柄'
      AND {exclude_cond}
    """


# ============================================================
# COUNTS / CHECKS
# ============================================================
def count_rows(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    if row is None:
        return 0
    return int(row[0])


def log_summary_counts(conn: sqlite3.Connection, table_name: str) -> None:
    total = count_rows(conn, f"SELECT COUNT(*) FROM {quote_ident(table_name)}")
    buy_cnt = count_rows(
        conn,
        f"SELECT COUNT(*) FROM {quote_ident(table_name)} WHERE buy_target = 1",
    )
    sell_cnt = count_rows(
        conn,
        f"SELECT COUNT(*) FROM {quote_ident(table_name)} WHERE sell_target = 1",
    )

    sell_non_margin = count_rows(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {quote_ident(table_name)}
        WHERE sell_target = 1
          AND COALESCE(credit_type, '') <> '貸借銘柄'
        """,
    )

    buy_bad_market = count_rows(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {quote_ident(table_name)}
        WHERE buy_target = 1
          AND market_type NOT IN ('プライム', 'スタンダード', 'グロース')
        """,
    )

    sell_bad_market = count_rows(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {quote_ident(table_name)}
        WHERE sell_target = 1
          AND market_type NOT IN ('プライム', 'スタンダード', 'グロース')
        """,
    )

    logger.info("summary total_rows=%d", total)
    logger.info("summary buy_target=1 count=%d", buy_cnt)
    logger.info("summary sell_target=1 count=%d", sell_cnt)
    logger.info("summary sell_target_non_margin count=%d", sell_non_margin)
    logger.info("summary buy_target_bad_market count=%d", buy_bad_market)
    logger.info("summary sell_target_bad_market count=%d", sell_bad_market)


def log_sample_rows(conn: sqlite3.Connection, table_name: str, limit: int = 20) -> None:
    #logger.info("---------- BUY TARGET SAMPLE ----------")
    rows = conn.execute(
        f"""
        SELECT symbol, symbolname, market_type, buy_target, sell_target, credit_type
        FROM {quote_ident(table_name)}
        WHERE buy_target = 1
        ORDER BY symbol
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()

    """for r in rows:
        logger.info(
            "BUY symbol=%s name=%s market=%s credit=%s",
            r["symbol"],
            r["symbolname"],
            r["market_type"],
            r["credit_type"],
        )"""

    #logger.info("---------- SELL TARGET SAMPLE ----------")
    rows = conn.execute(
        f"""
        SELECT symbol, symbolname, market_type, buy_target, sell_target, credit_type
        FROM {quote_ident(table_name)}
        WHERE sell_target = 1
        ORDER BY symbol
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()

    """for r in rows:
        logger.info(
            "SELL symbol=%s name=%s market=%s credit=%s",
            r["symbol"],
            r["symbolname"],
            r["market_type"],
            r["credit_type"],
        )"""


# ============================================================
# MAIN UPDATE
# ============================================================
def update_symbol_flags_targets(
    db_path: str,
    table_name: str = TABLE_NAME,
    dry_run: bool = False,
    create_backup: bool = True,
) -> None:
    db_path = str(db_path)

    if not Path(db_path).exists():
        raise FileNotFoundError(f"DB file not found: {db_path}")

    if create_backup and not dry_run:
        backup_db_file(db_path)

    with connect_sqlite(db_path) as conn:
        if not table_exists(conn, table_name):
            raise RuntimeError(f"table not found: {table_name} in db: {db_path}")

        cols = ensure_required_columns(conn, table_name)
        logger.info("db=%s", db_path)
        logger.info("table=%s columns=%s", table_name, sorted(cols))

        reset_sql = build_reset_sql(table_name)
        buy_sql = build_buy_update_sql(table_name)
        sell_sql = build_sell_update_sql(table_name)

        logger.info("before update")
        log_summary_counts(conn, table_name)

        if dry_run:
            logger.info("[DRY RUN] reset sql:\n%s", reset_sql.strip())
            logger.info("[DRY RUN] buy update sql:\n%s", buy_sql.strip())
            logger.info("[DRY RUN] sell update sql:\n%s", sell_sql.strip())
            return

        try:
            conn.execute("BEGIN")
            conn.execute(reset_sql)
            conn.execute(buy_sql)
            conn.execute(sell_sql)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("update failed and rolled back db=%s table=%s", db_path, table_name)
            raise

        logger.info("after update")
        log_summary_counts(conn, table_name)
        log_sample_rows(conn, table_name, limit=20)


# ============================================================
# COMPAT ENTRYPOINT
# ============================================================
def update_symbol_flags(*args, **kwargs):
    return update_symbol_flags_targets(*args, **kwargs)


# ============================================================
# CLI
# ============================================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update symbol_flags buy_target / sell_target by market_type and credit_type"
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help="Path to symbol_flags.db",
    )
    parser.add_argument(
        "--table",
        default=TABLE_NAME,
        help="Target table name",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print SQL / counts without updating DB",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create .bak backup file before update",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(verbose=bool(args.verbose))

    update_symbol_flags_targets(
        db_path=args.db_path,
        table_name=args.table,
        dry_run=bool(args.dry_run),
        create_backup=not bool(args.no_backup),
    )


if __name__ == "__main__":
    main()