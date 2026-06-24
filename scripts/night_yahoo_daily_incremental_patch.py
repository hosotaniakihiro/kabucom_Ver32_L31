# ============================================================
# File   : scripts/night_yahoo_daily_incremental_patch.py
# Version: V1-NIGHT-YAHOO-DAILY-INCREMENTAL-FROM-DB
# ------------------------------------------------------------
# 夜間Yahoo日足バッチを、DB最新日以降だけ取得・計算・保存する
# 差分更新方式へ差し替えるパッチ。
#
# 方針:
#   1) stock_analysis_latest / history から銘柄ごとの最新日を取得
#   2) Yahooから最新日の翌日以降だけ取得
#   3) 指標計算用にDB historyから直近N本をウォームアップとして読む
#   4) 計算後、DBに未格納の日付分だけ history/latest へupsert
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pandas as pd

LOG = logging.getLogger("night_yahoo_daily_incremental_patch")
VERSION = "V1-NIGHT-YAHOO-DAILY-INCREMENTAL-FROM-DB"
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


def install(daily_mod: Any) -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if daily_mod is None:
        return False
    if getattr(daily_mod, "_NIGHT_YAHOO_DAILY_INCREMENTAL_PATCHED", False):
        _INSTALLED = True
        return True

    original_process_symbol = getattr(daily_mod, "process_symbol", None)
    if not callable(original_process_symbol):
        LOG.warning("[NIGHT YAHOO DAILY INCR] install failed: process_symbol missing")
        return False

    def process_symbol_incremental(rec: dict[str, str], *, period: str, start: Optional[str], db_path: Path):
        symbol = _normalize_symbol(rec.get("symbol", ""))
        if not symbol:
            return symbol, False, "empty symbol", 0

        force_full = str(os.environ.get("NIGHT_YAHOO_DAILY_FORCE_FULL", "0")).strip().lower() in {"1", "true", "yes", "on"}
        warmup_rows = int(float(os.environ.get("NIGHT_YAHOO_DAILY_WARMUP_ROWS", "320")))

        latest_dt = None if force_full else _get_latest_date(Path(db_path), symbol, daily_mod)

        # 明示 start がある場合は従来どおりその日から。なければDB最新日の翌日から差分取得。
        effective_start = start
        if latest_dt is not None and not effective_start:
            effective_start = _date_str(latest_dt + pd.Timedelta(days=1))

        try:
            raw_new = daily_mod._fetch_daily_one(symbol, period=period, start=effective_start)
            if raw_new is None or raw_new.empty:
                if latest_dt is not None:
                    return symbol, True, f"up-to-date latest={_date_str(latest_dt)}", 0
                return symbol, False, "no daily data", 0

            raw_new["date"] = pd.to_datetime(raw_new["date"], errors="coerce").dt.normalize()
            raw_new = raw_new.dropna(subset=["date"])

            if latest_dt is not None:
                raw_new = raw_new[raw_new["date"] > latest_dt].copy()
                if raw_new.empty:
                    return symbol, True, f"up-to-date latest={_date_str(latest_dt)}", 0

            warmup = pd.DataFrame()
            if latest_dt is not None:
                warmup = _load_warmup_prices(Path(db_path), symbol, daily_mod, warmup_rows)

            calc_prices = _merge_prices(warmup, raw_new, symbol)
            if calc_prices.empty:
                return symbol, False, "no calc prices", 0

            computed = daily_mod._run_indicator_pipeline(calc_prices, rec)
            if computed.empty:
                return symbol, False, "computed empty", 0

            computed["date"] = pd.to_datetime(computed["date"], errors="coerce")
            computed = computed.dropna(subset=["date"])
            if latest_dt is not None:
                save_df = computed[computed["date"].dt.normalize() > latest_dt].copy()
            else:
                save_df = computed.copy()

            if save_df.empty:
                return symbol, True, f"up-to-date latest={_date_str(latest_dt)}", 0

            save_df["date"] = save_df["date"].dt.strftime("%Y-%m-%d")
            hist, lat = daily_mod._save_symbol_df(Path(db_path), save_df)
            new_min = pd.to_datetime(save_df["date"], errors="coerce").min()
            new_max = pd.to_datetime(save_df["date"], errors="coerce").max()
            return symbol, True, f"incremental saved new_rows={hist} latest={lat} range={_date_str(new_min)}..{_date_str(new_max)} db_latest_before={_date_str(latest_dt) if latest_dt is not None else '-'}", hist
        except Exception as e:
            LOG.warning("[NIGHT YAHOO DAILY INCR] symbol failed symbol=%s err=%s", symbol, e, exc_info=True)
            return symbol, False, str(e), 0

    daily_mod.process_symbol = process_symbol_incremental
    daily_mod._NIGHT_YAHOO_DAILY_INCREMENTAL_PATCHED = True
    # 表示用のversionも上書きして、ログで差分版だと分かるようにする。
    try:
        daily_mod.VERSION = "V3-NIGHT-YAHOO-DAILY-INCREMENTAL-DB"
    except Exception:
        pass
    _INSTALLED = True
    LOG.warning(
        "[NIGHT YAHOO DAILY INCR] installed version=%s warmup_rows=%s force_full=%s",
        VERSION,
        os.environ.get("NIGHT_YAHOO_DAILY_WARMUP_ROWS", "320"),
        os.environ.get("NIGHT_YAHOO_DAILY_FORCE_FULL", "0"),
    )
    return True
