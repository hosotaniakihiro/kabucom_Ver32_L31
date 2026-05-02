# ============================================================
# File   : tools/diagnose_summary_db_schema.py
# Ver    : DIAG-SUMMARY-DB-SCHEMA-AND-FILL-RATE-REV1.0
# ------------------------------------------------------------
# 【目的】
#   summaryYYYYMMDD.db の実スキーマと、各列の格納状況を確認する。
#
# 【確認内容】
#   1. DBファイル存在確認
#   2. テーブル一覧
#   3. stock_summary_1min / 3min / 5min の PRAGMA table_info
#   4. 期待列がDBに存在するか
#   5. 各列の NULL件数 / 0件数 / 非NULL件数 / 非0件数
#   6. 最新時刻・銘柄数・行数
#   7. サンプル最新行
#
# 【使い方】
#   python tools/diagnose_summary_db_schema.py
#
#   日付を指定する場合:
#   python tools/diagnose_summary_db_schema.py 20260424
# ============================================================

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path
from typing import Any


# ============================================================
# CONFIG
# ============================================================

SUMMARY_DB_DIR = Path(
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary"
)

TABLES = [
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
]

EXPECTED_COLUMNS = [
    # identity
    "id",
    "symbol",
    "symbolname",
    "datetime",
    "date",
    "time",
    "time_range",
    "source",
    "last_update",

    # OHLCV
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "open",
    "high",
    "low",
    "close",
    "volume",

    # indicators
    "vwap",
    "ma5",
    "ma25",
    "ma75",
    "ma5_conf",
    "ma25_conf",
    "ma75_conf",
    "ma75_slope",
    "volume_slope",
    "vwap_slope",
    "slope",
    "slope_atr_scaled",
    "score_slope",

    "ema12",
    "ema26",
    "macd",
    "signal",
    "hist",
    "rsi",
    "rci",
    "atr",

    "bb_mid",
    "bb_upper",
    "bb_lower",
    "bb_width",

    # scores
    "score",
    "score_buy",
    "score_sell",
    "score_total",
    "final_score",
    "display_score",
    "score_mtf",
    "mtf",
    "mtf_score",

    # score breakdown
    "base",
    "trend",
    "mom",
    "vel",
    "pen",

    # ranking / display optional
    "rank",
    "ranking",
    "turnover",
    "tick_count",
]


# ============================================================
# LOG
# ============================================================

def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe = str(msg).encode("cp932", errors="ignore").decode("cp932")
    print(f"[{ts}] {safe}", flush=True)


# ============================================================
# DATE
# ============================================================

def prev_business_day(base_date: dt.date | None = None) -> dt.date:
    d = (base_date or dt.date.today()) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def resolve_yyyymmdd() -> str:
    if len(sys.argv) >= 2:
        raw = str(sys.argv[1]).strip()
        if len(raw) == 8 and raw.isdigit():
            return raw
        raise SystemExit("日付は YYYYMMDD 形式で指定してください。例: 20260424")

    # 引数なしなら前営業日
    return prev_business_day().strftime("%Y%m%d")


# ============================================================
# SQLITE HELPERS
# ============================================================

def qvalue(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cur = con.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def qrows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    cur = con.execute(sql, params)
    return cur.fetchall()


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    n = qvalue(
        con,
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return int(n or 0) > 0


def get_table_columns(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = qrows(con, f"PRAGMA table_info({table})")
    return [
        {
            "cid": r["cid"],
            "name": r["name"],
            "type": r["type"],
            "notnull": r["notnull"],
            "default": r["dflt_value"],
            "pk": r["pk"],
        }
        for r in rows
    ]


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def is_numeric_type(type_name: str) -> bool:
    t = str(type_name or "").upper()
    return any(k in t for k in ["INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"])


def safe_count_expr(col: str, type_name: str) -> str:
    q = quote_ident(col)

    if is_numeric_type(type_name):
        return f"""
            SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS null_count,
            SUM(CASE WHEN {q} IS NOT NULL THEN 1 ELSE 0 END) AS notnull_count,
            SUM(CASE WHEN {q} = 0 THEN 1 ELSE 0 END) AS zero_count,
            SUM(CASE WHEN {q} IS NOT NULL AND {q} != 0 THEN 1 ELSE 0 END) AS nonzero_count,
            MIN({q}) AS min_value,
            MAX({q}) AS max_value
        """

    return f"""
        SUM(CASE WHEN {q} IS NULL OR TRIM(CAST({q} AS TEXT)) = '' THEN 1 ELSE 0 END) AS null_count,
        SUM(CASE WHEN {q} IS NOT NULL AND TRIM(CAST({q} AS TEXT)) != '' THEN 1 ELSE 0 END) AS notnull_count,
        NULL AS zero_count,
        NULL AS nonzero_count,
        MIN({q}) AS min_value,
        MAX({q}) AS max_value
    """


# ============================================================
# DIAG
# ============================================================

def print_table_basic(con: sqlite3.Connection, table: str) -> None:
    total = qvalue(con, f"SELECT COUNT(*) FROM {quote_ident(table)}")
    symbols = qvalue(con, f"SELECT COUNT(DISTINCT symbol) FROM {quote_ident(table)}") \
        if has_column(con, table, "symbol") else None

    min_dt = qvalue(con, f"SELECT MIN(datetime) FROM {quote_ident(table)}") \
        if has_column(con, table, "datetime") else None
    max_dt = qvalue(con, f"SELECT MAX(datetime) FROM {quote_ident(table)}") \
        if has_column(con, table, "datetime") else None

    log(f"[BASIC] table={table} rows={total} symbols={symbols} min_dt={min_dt} max_dt={max_dt}")


def has_column(con: sqlite3.Connection, table: str, col: str) -> bool:
    cols = get_table_columns(con, table)
    return col in {c["name"] for c in cols}


def print_schema(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    cols = get_table_columns(con, table)

    log("")
    log("=" * 90)
    log(f"[SCHEMA] {table}")
    log("=" * 90)

    for c in cols:
        log(
            f"  {c['cid']:>3} | {c['name']:<24} | {c['type']:<12} "
            f"| notnull={c['notnull']} | pk={c['pk']} | default={c['default']}"
        )

    return cols


def print_missing_expected(table: str, cols: list[dict[str, Any]]) -> None:
    actual = {c["name"] for c in cols}
    missing = [c for c in EXPECTED_COLUMNS if c not in actual]
    extra = [c for c in sorted(actual) if c not in EXPECTED_COLUMNS]

    log("")
    log(f"[EXPECTED CHECK] {table}")
    log(f"  expected columns count = {len(EXPECTED_COLUMNS)}")
    log(f"  actual columns count   = {len(actual)}")

    if missing:
        log("  [MISSING IN DB]")
        for c in missing:
            log(f"    - {c}")
    else:
        log("  [MISSING IN DB] none")

    if extra:
        log("  [EXTRA IN DB]")
        for c in extra:
            log(f"    + {c}")
    else:
        log("  [EXTRA IN DB] none")


def print_fill_rate(con: sqlite3.Connection, table: str, cols: list[dict[str, Any]]) -> None:
    total = int(qvalue(con, f"SELECT COUNT(*) FROM {quote_ident(table)}") or 0)

    log("")
    log(f"[FILL RATE] {table} total_rows={total}")

    if total <= 0:
        log("  no rows")
        return

    log(
        "  column                   type         null     notnull  null%     "
        "zero     nonzero  min                 max"
    )
    log("  " + "-" * 120)

    for c in cols:
        col = c["name"]
        typ = c["type"]

        # id等は軽く見るだけでよい
        sql = f"SELECT {safe_count_expr(col, typ)} FROM {quote_ident(table)}"
        row = qrows(con, sql)[0]

        null_count = int(row["null_count"] or 0)
        notnull_count = int(row["notnull_count"] or 0)
        zero_count = row["zero_count"]
        nonzero_count = row["nonzero_count"]

        null_pct = null_count / total * 100 if total else 0

        min_value = row["min_value"]
        max_value = row["max_value"]

        log(
            f"  {col:<24} {typ:<12} "
            f"{null_count:>8} {notnull_count:>8} {null_pct:>6.1f}% "
            f"{str(zero_count):>8} {str(nonzero_count):>8} "
            f"{str(min_value)[:18]:<18} {str(max_value)[:18]:<18}"
        )


def print_latest_samples(con: sqlite3.Connection, table: str) -> None:
    if not has_column(con, table, "datetime"):
        return

    sample_cols = [
        c for c in [
            "symbol", "symbolname", "datetime",
            "open_price", "high_price", "low_price", "close_price", "volume",
            "ma5", "ma25", "ma75",
            "rsi", "macd", "signal", "hist",
            "atr", "rci", "vwap",
            "bb_width",
            "score", "score_buy", "score_sell", "score_total",
            "final_score", "display_score",
            "slope", "slope_atr_scaled", "score_slope",
            "mtf", "score_mtf",
            "source",
        ]
        if has_column(con, table, c)
    ]

    if not sample_cols:
        return

    sql = f"""
        SELECT {", ".join(quote_ident(c) for c in sample_cols)}
        FROM {quote_ident(table)}
        ORDER BY datetime DESC
        LIMIT 5
    """

    rows = qrows(con, sql)

    log("")
    log(f"[LATEST SAMPLE] {table}")

    for r in rows:
        parts = []
        for c in sample_cols:
            v = r[c]
            parts.append(f"{c}={v}")
        log("  " + " | ".join(parts))


def print_index_info(con: sqlite3.Connection, table: str) -> None:
    rows = qrows(con, f"PRAGMA index_list({quote_ident(table)})")

    log("")
    log(f"[INDEX] {table}")

    if not rows:
        log("  no indexes")
        return

    for r in rows:
        idx_name = r["name"]
        unique = r["unique"]
        origin = r["origin"]
        partial = r["partial"]
        log(f"  index={idx_name} unique={unique} origin={origin} partial={partial}")

        cols = qrows(con, f"PRAGMA index_info({quote_ident(idx_name)})")
        for c in cols:
            log(f"    - seq={c['seqno']} cid={c['cid']} name={c['name']}")


def print_problem_summary(con: sqlite3.Connection, table: str, cols: list[dict[str, Any]]) -> None:
    total = int(qvalue(con, f"SELECT COUNT(*) FROM {quote_ident(table)}") or 0)
    if total <= 0:
        return

    actual = {c["name"]: c["type"] for c in cols}

    important = [
        "ma5", "ma25", "ma75",
        "rsi", "macd", "signal", "hist",
        "atr", "rci", "vwap",
        "bb_mid", "bb_upper", "bb_lower", "bb_width",
        "ma5_conf", "ma25_conf", "ma75_conf",
        "ma75_slope", "volume_slope", "vwap_slope",
        "slope", "slope_atr_scaled", "score_slope",
        "score", "score_buy", "score_sell", "score_total",
        "final_score", "display_score",
        "mtf", "score_mtf",
    ]

    log("")
    log(f"[PROBLEM SUMMARY] {table}")

    for col in important:
        if col not in actual:
            log(f"  MISSING_COLUMN  {col}")
            continue

        q = quote_ident(col)
        sql = f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS null_count,
                SUM(CASE WHEN {q} = 0 THEN 1 ELSE 0 END) AS zero_count,
                SUM(CASE WHEN {q} IS NOT NULL AND {q} != 0 THEN 1 ELSE 0 END) AS nonzero_count
            FROM {quote_ident(table)}
        """
        row = qrows(con, sql)[0]

        null_count = int(row["null_count"] or 0)
        zero_count = int(row["zero_count"] or 0)
        nonzero_count = int(row["nonzero_count"] or 0)

        if null_count == total:
            status = "ALL_NULL"
        elif zero_count == total:
            status = "ALL_ZERO"
        elif nonzero_count == 0:
            status = "NO_NONZERO"
        elif null_count > 0:
            status = "PARTIAL_NULL"
        else:
            status = "OK"

        log(
            f"  {status:<14} {col:<18} "
            f"null={null_count}/{total} zero={zero_count}/{total} nonzero={nonzero_count}/{total}"
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    yyyymmdd = resolve_yyyymmdd()
    db_path = SUMMARY_DB_DIR / f"summary{yyyymmdd}.db"

    log("SUMMARY DB DIAG START")
    log(f"target_date = {yyyymmdd}")
    log(f"db_path     = {db_path}")

    if not db_path.exists():
        log(f"[ERROR] DB file not found: {db_path}")
        return

    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row

    try:
        log("")
        log("[TABLE LIST]")
        tables = qrows(
            con,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        for r in tables:
            log(f"  - {r['name']}")

        for table in TABLES:
            log("")
            log("#" * 90)
            log(f"# CHECK TABLE: {table}")
            log("#" * 90)

            if not table_exists(con, table):
                log(f"[MISSING TABLE] {table}")
                continue

            print_table_basic(con, table)
            cols = print_schema(con, table)
            print_missing_expected(table, cols)
            print_index_info(con, table)
            print_fill_rate(con, table, cols)
            print_problem_summary(con, table, cols)
            print_latest_samples(con, table)

    finally:
        con.close()

    log("")
    log("SUMMARY DB DIAG END")


if __name__ == "__main__":
    main()