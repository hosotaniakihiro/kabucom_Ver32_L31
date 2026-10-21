# ============================================================
# File   : scripts/night_yahoo_daily_incremental.py
# Version: V2-NIGHT-YAHOO-DAILY-INCREMENTAL-END-DATE
# ------------------------------------------------------------
# 夜間Yahoo日足バッチを、DB最新日以降だけ取得・計算・保存する
# 差分更新方式へ差し替えるパッチ。
#
# 方針:
#   1) stock_analysis_latest / history から銘柄ごとの最新日を取得
#   2) Yahooから最新日の翌日以降だけ取得
#   3) NIGHT_YAHOO_DAILY_END_DATE があれば、その日付までに制限
#   4) 指標計算用にDB historyから直近N本をウォームアップとして読む
#   5) 計算後、DBに未格納の日付分だけ history/latest へupsert
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pandas as pd

LOG = logging.getLogger("night_yahoo_daily_incremental")
VERSION = "V2-NIGHT-YAHOO-DAILY-INCREMENTAL-END-DATE"
_INSTALLED = False


def _date_str(dt: Any) -> str:
    return pd.Timestamp(dt).strftime("%Y-%m-%d")


def _normalize_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(4) if s.isdigit() and len(s) < 4 else s


def _parse_end_date() -> Optional[pd.Timestamp]:
    raw = str(os.environ.get("NIGHT_YAHOO_DAILY_END_DATE", "")).strip()
    if not raw:
        return None
    try:
        return pd.Timestamp(raw).normalize()
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY INCR] invalid NIGHT_YAHOO_DAILY_END_DATE=%s ignored", raw)
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None
    except Exception:
        return False


def _get_latest_date(db_path: Path, symbol: str, daily_mod: Any) -> Optional[pd.Timestamp]:
    symbol = _normalize_symbol(symbol)
    if not db_path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        latest_table = getattr(daily_mod, "DB_TABLE_LATEST", "stock_analysis_latest")
        history_table = getattr(daily_mod, "DB_TABLE_HISTORY", "stock_analysis_history")

        if _table_exists(conn, latest_table):
            row = conn.execute(
                f'SELECT MAX("date") FROM {latest_table} WHERE "stock_code"=?',
                (symbol,),
            ).fetchone()
            if row and row[0]:
                return pd.Timestamp(row[0]).normalize()

        if _table_exists(conn, history_table):
            row = conn.execute(
                f'SELECT MAX("date") FROM {history_table} WHERE "stock_code"=?',
                (symbol,),
            ).fetchone()
            if row and row[0]:
                return pd.Timestamp(row[0]).normalize()
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY INCR] latest date lookup failed symbol=%s db=%s", symbol, db_path, exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def _load_warmup_prices(db_path: Path, symbol: str, daily_mod: Any, rows: int) -> pd.DataFrame:
    symbol = _normalize_symbol(symbol)
    if rows <= 0 or not db_path.exists():
        return pd.DataFrame()
    history_table = getattr(daily_mod, "DB_TABLE_HISTORY", "stock_analysis_history")
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=20)
        if not _table_exists(conn, history_table):
            return pd.DataFrame()
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({history_table})").fetchall()}
        needed = ["stock_code", "date", "open", "high", "low", "close", "volume"]
        if not all(c in cols for c in needed):
            return pd.DataFrame()
        adj_expr = '"adj_close"' if "adj_close" in cols else '"close" AS "adj_close"'
        sql = f'''
            SELECT "stock_code", "date", "open", "high", "low", "close", {adj_expr}, "volume"
            FROM {history_table}
            WHERE "stock_code"=?
            ORDER BY "date" DESC
            LIMIT ?
        '''
        df = pd.read_sql_query(sql, conn, params=(symbol, int(rows)))
        if df.empty:
            return df
        df = df.rename(columns={"stock_code": "symbol"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
        for c in ["open", "high", "low", "close", "adj_close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["date", "open", "high", "low", "close"])
        df["volume"] = df["volume"].fillna(0)
        df["symbol"] = symbol
        return df.sort_values("date")[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]].copy()
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY INCR] warmup load failed symbol=%s db=%s", symbol, db_path, exc_info=True)
        return pd.DataFrame()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _merge_prices(warmup: pd.DataFrame, new_rows: pd.DataFrame, symbol: str) -> pd.DataFrame:
    frames = []
    if warmup is not None and not warmup.empty:
        frames.append(warmup)
    if new_rows is not None and not new_rows.empty:
        frames.append(new_rows)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["symbol"] = _normalize_symbol(symbol)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    return df.sort_values("date").reset_index(drop=True)

date_str = _date_str
normalize_symbol = _normalize_symbol
parse_end_date = _parse_end_date
get_latest_date = _get_latest_date
load_warmup_prices = _load_warmup_prices
merge_prices = _merge_prices

__all__ = [
    "VERSION",
    "date_str",
    "normalize_symbol",
    "parse_end_date",
    "get_latest_date",
    "load_warmup_prices",
    "merge_prices",
]

