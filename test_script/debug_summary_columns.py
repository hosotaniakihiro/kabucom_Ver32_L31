# ============================================================
# File   : debug_summary_columns.py
# Ver    : DEBUG-SUMMARY-COLUMNS-CHECK-V1.0
# ------------------------------------------------------------
# ✔ 定時サマリー結果の「項目一覧」を確認するためのデバッグ用
# ✔ summary DB / ranking DB / global_data の列名を確認
# ✔ 最新数行も表示して中身を確認
# ============================================================

from __future__ import annotations

import os
import sys
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Optional, Iterable

import pandas as pd


# ------------------------------------------------------------
# 環境に合わせて必要なら変更
# ------------------------------------------------------------
BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station"
SUMMARY_DIR = os.path.join(BASE_DIR, "summary")
RANKING_DIR = os.path.join(BASE_DIR, "ranking")


# ------------------------------------------------------------
# 汎用
# ------------------------------------------------------------
def today_str() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def prev_business_day_str(days_back: int = 1) -> str:
    d = dt.datetime.now().date()
    cnt = 0
    while cnt < days_back:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            cnt += 1
    return d.strftime("%Y%m%d")


def resolve_existing_db(dir_path: str, prefix: str, prefer_today: bool = True) -> Optional[str]:
    candidates = []

    if prefer_today:
        today_file = os.path.join(dir_path, f"{prefix}{today_str()}.db")
        if os.path.exists(today_file):
            return today_file

    prev_file = os.path.join(dir_path, f"{prefix}{prev_business_day_str(1)}.db")
    if os.path.exists(prev_file):
        candidates.append(prev_file)

    try:
        for name in sorted(os.listdir(dir_path), reverse=True):
            if name.startswith(prefix) and name.endswith(".db"):
                candidates.append(os.path.join(dir_path, name))
    except Exception:
        pass

    for p in candidates:
        if os.path.exists(p):
            return p

    return None


def print_header(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def print_sub(title: str) -> None:
    print()
    print("-" * 80)
    print(title)
    print("-" * 80)


def safe_read_sql(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, conn)
    except Exception as e:
        print(f"[ERROR] read_sql failed: {e}")
        return pd.DataFrame()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    df = safe_read_sql(
        conn,
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
        ORDER BY name
        """
    )
    if df.empty or "name" not in df.columns:
        return []
    return df["name"].astype(str).tolist()


def table_columns(conn: sqlite3.Connection, table_name: str) -> pd.DataFrame:
    return safe_read_sql(conn, f"PRAGMA table_info({table_name})")


def latest_rows(conn: sqlite3.Connection, table_name: str, limit: int = 3) -> pd.DataFrame:
    cols_df = table_columns(conn, table_name)
    if cols_df.empty:
        return pd.DataFrame()

    colnames = cols_df["name"].astype(str).tolist()
    order_col = None
    for c in ["datetime", "timestamp", "created_at", "updated_at", "date"]:
        if c in colnames:
            order_col = c
            break

    sql = f"SELECT * FROM {table_name}"
    if order_col:
        sql += f" ORDER BY {order_col} DESC"
    sql += f" LIMIT {int(limit)}"

    return safe_read_sql(conn, sql)


def print_table_info(conn: sqlite3.Connection, table_name: str, latest_limit: int = 3) -> None:
    print_sub(f"TABLE: {table_name}")

    cols = table_columns(conn, table_name)
    if cols.empty:
        print("列情報を取得できませんでした。")
        return

    if "name" in cols.columns:
        column_names = cols["name"].astype(str).tolist()
        print("columns:")
        print(", ".join(column_names))
        print(f"column_count={len(column_names)}")

    print()
    print("schema:")
    try:
        show_cols = [c for c in ["cid", "name", "type", "notnull", "dflt_value", "pk"] if c in cols.columns]
        print(cols[show_cols].to_string(index=False))
    except Exception:
        print(cols.to_string(index=False))

    rows = latest_rows(conn, table_name, latest_limit)
    print()
    print(f"latest_rows(limit={latest_limit}):")
    if rows.empty:
        print("(no rows)")
    else:
        with pd.option_context("display.max_columns", None, "display.width", 200):
            print(rows.to_string(index=False))


def inspect_db(db_path: str, target_tables: Optional[Iterable[str]] = None, latest_limit: int = 3) -> None:
    print_header(f"DB INSPECT: {db_path}")

    if not db_path or not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    try:
        tables = list_tables(conn)
        print(f"tables({len(tables)}):")
        for t in tables:
            print(f" - {t}")

        if target_tables:
            use_tables = [t for t in target_tables if t in tables]
        else:
            use_tables = tables

        for t in use_tables:
            print_table_info(conn, t, latest_limit=latest_limit)

    finally:
        conn.close()


# ------------------------------------------------------------
# global_data 側の確認
# ------------------------------------------------------------
def try_import_global_data():
    tried = []

    candidates = [
        ("global_data", "global_data"),
        ("global_state", "global_data"),
        ("core.global_context", "global_data"),
        ("core.global_context.context", "global_data"),
    ]

    for module_name, attr_name in candidates:
        try:
            mod = __import__(module_name, fromlist=[attr_name])
            obj = getattr(mod, attr_name, None)
            if obj is not None:
                return obj
            tried.append(f"{module_name}.{attr_name}=None")
        except Exception as e:
            tried.append(f"{module_name}: {e}")

    print_sub("global_data import failed")
    for x in tried:
        print(" -", x)
    return None


def try_get_merged_summary(global_data_obj, tf: int):
    getters = [
        "get_merged_summary",
        "get_summary",
    ]
    for name in getters:
        fn = getattr(global_data_obj, name, None)
        if callable(fn):
            try:
                return fn(tf)
            except TypeError:
                try:
                    return fn(interval=tf)
                except Exception:
                    pass
            except Exception:
                pass

    for attr in [
        f"merged_summary_{tf}m",
        f"summary_{tf}m",
        "merged_summaries",
        "summary_cache",
    ]:
        try:
            value = getattr(global_data_obj, attr, None)
            if isinstance(value, dict):
                if tf in value:
                    return value[tf]
                if str(tf) in value:
                    return value[str(tf)]
            elif value is not None and tf in (1, 3, 5):
                return value
        except Exception:
            pass

    return None


def inspect_global_data() -> None:
    print_header("GLOBAL_DATA INSPECT")

    gd = try_import_global_data()
    if gd is None:
        print("global_data を取得できませんでした。")
        return

    for tf in [1, 3, 5]:
        print_sub(f"global_data merged summary tf={tf}")
        df = try_get_merged_summary(gd, tf)

        if df is None:
            print("df is None")
            continue

        if not isinstance(df, pd.DataFrame):
            print(f"not a DataFrame: type={type(df)}")
            continue

        print(f"rows={len(df)}")
        print(f"columns({len(df.columns)}):")
        print(", ".join(map(str, df.columns.tolist())))

        if not df.empty:
            with pd.option_context("display.max_columns", None, "display.width", 220):
                print()
                print(df.head(3).to_string(index=False))


# ------------------------------------------------------------
# サマリーで見たい主要列
# ------------------------------------------------------------
def print_expected_columns_guide() -> None:
    print_header("EXPECTED SUMMARY COLUMNS GUIDE")

    push_cols = [
        "symbol", "symbolname", "close", "score",
        "buy_score", "sell_score",
        "slope", "score_slope",
        "mtf", "score_mtf",
        "total_score", "final_score",
        "rsi", "macd",
        "base_score", "trend_score", "momentum_score", "velocity_score", "penalty_score",
        "signal_score",
        "datetime",
    ]

    ranking_cols = [
        "symbol", "symbolname", "close", "score",
        "slope", "rsi", "macd",
        "best_rank", "hist", "ranking_type",
        "datetime",
    ]

    print("PUSH由来サマリーでよく使う列:")
    print(", ".join(push_cols))
    print()
    print("ランキング由来サマリーでよく使う列:")
    print(", ".join(ranking_cols))


# ------------------------------------------------------------
# main
# ------------------------------------------------------------
def main() -> None:
    print_expected_columns_guide()

    summary_db = resolve_existing_db(SUMMARY_DIR, "summary", prefer_today=True)
    ranking_db = resolve_existing_db(RANKING_DIR, "ranking", prefer_today=True)

    print_header("RESOLVED DB PATHS")
    print("summary_db =", summary_db)
    print("ranking_db =", ranking_db)

    if summary_db:
        inspect_db(
            summary_db,
            target_tables=[
                "stock_summary_1min",
                "stock_summary_3min",
                "stock_summary_5min",
                "stock_summary_10min",
                "stock_summary_15min",
                "stock_summary_30min",
                "stock_summary_60min",
                "stock_summary_daily",
            ],
            latest_limit=5,
        )

    if ranking_db:
        inspect_db(
            ranking_db,
            target_tables=[
                "ranking_snapshot_1min",
                "ranking_raw_1min",
                "ranking_ma_1min",
            ],
            latest_limit=5,
        )

    inspect_global_data()


if __name__ == "__main__":
    main()