# ============================================================
# File   : trading/summary/pipeline/incremental_mtf_from_1min.py
# Version: V1-INCREMENTAL-MTF-FROM-1M-DB
# ------------------------------------------------------------
# 目的:
#   3分足/5分足の最新保存済みサマリー時刻を見て、
#   それ以降の1分足を summary DB から読み込み、差分の3m/5mサマリーを作る。
#
# 方針:
#   - 3m/5m側は MA75 計算に必要な直前履歴を読む
#   - 直前履歴は既定74本、差分バーを足して indicator/scoring に渡す
#   - DB保存対象は「最新3m/5m時刻より後の差分バー」だけ
#   - main.pyの起動時seed restoreのように全銘柄75本×全足を毎回読まない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_TABLES = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _summary_db_path() -> Path | None:
    try:
        import database.session as ds

        engine = getattr(ds, "summary_engine", None) or getattr(ds, "_summary_engine", None)
        db = getattr(getattr(engine, "url", None), "database", None) if engine is not None else None
        if db:
            return Path(str(db))
        url = str(getattr(getattr(engine, "url", None), "__str__", lambda: "")() if engine is not None else "")
        if url.startswith("sqlite:///"):
            return Path(url.replace("sqlite:///", "", 1))
    except Exception:
        logger.exception("[MTF DIFF 1M] summary db path resolve failed")
    return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()]
    except Exception:
        logger.exception("[MTF DIFF 1M] table_info failed table=%s", table)
        return []


def _first_existing(cols: set[str], names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in cols:
            return n
    return None


def _normalize_symbol_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)


def _normalize_summary_df(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out = out.loc[:, ~out.columns.duplicated()].copy()
    if "__rn" in out.columns:
        out = out.drop(columns=["__rn"], errors="ignore")
    if "symbol" not in out.columns:
        return pd.DataFrame()
    out["symbol"] = _normalize_symbol_series(out["symbol"])
    if "datetime" not in out.columns:
        for c in ("end_time", "time", "start_time"):
            if c in out.columns:
                out["datetime"] = out[c]
                break
    if "datetime" not in out.columns:
        return pd.DataFrame()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    out = out[out["symbol"].astype(str).str.strip() != ""]
    if out.empty:
        return pd.DataFrame()
    alias_pairs = (
        ("open", "open_price"),
        ("high", "high_price"),
        ("low", "low_price"),
        ("close", "close_price"),
    )
    for a, b in alias_pairs:
        if a not in out.columns and b in out.columns:
            out[a] = out[b]
        if b not in out.columns and a in out.columns:
            out[b] = out[a]
    if "close" not in out.columns and "price" in out.columns:
        out["close"] = out["price"]
    if "close_price" not in out.columns and "close" in out.columns:
        out["close_price"] = out["close"]
    for c in (
        "open", "high", "low", "close", "open_price", "high_price", "low_price", "close_price",
        "volume", "trading_volume", "trading_value", "turnover", "tick_count",
        "ma5", "ma25", "ma75", "rsi", "macd", "signal", "hist", "slope", "slope_atr_scaled",
    ):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "source" not in out.columns:
        out["source"] = "push"
    out["interval"] = int(interval)
    return out.sort_values(["symbol", "datetime"], kind="stable").drop_duplicates(["symbol", "datetime"], keep="last").reset_index(drop=True)


def _read_latest_dt(conn: sqlite3.Connection, table: str) -> pd.Timestamp | None:
    try:
        if not _table_exists(conn, table):
            return None
        cols = set(_table_columns(conn, table))
        dt_col = _first_existing(cols, ("datetime", "end_time", "time", "start_time"))
        if not dt_col:
            return None
        row = conn.execute(f"SELECT MAX({_quote_ident(dt_col)}) FROM {_quote_ident(table)}").fetchone()
        if not row or row[0] is None:
            return None
        ts = pd.to_datetime(row[0], errors="coerce")
        if pd.isna(ts):
            return None
        return pd.Timestamp(ts).tz_localize(None) if getattr(pd.Timestamp(ts), "tz", None) else pd.Timestamp(ts)
    except Exception:
        logger.exception("[MTF DIFF 1M] latest dt read failed table=%s", table)
        return None


def _read_history_before(conn: sqlite3.Connection, table: str, *, latest_dt: pd.Timestamp, rows_per_symbol: int) -> pd.DataFrame:
    if not _table_exists(conn, table):
        return pd.DataFrame()
    cols = set(_table_columns(conn, table))
    dt_col = _first_existing(cols, ("datetime", "end_time", "time", "start_time"))
    if not dt_col or "symbol" not in cols:
        return pd.DataFrame()
    q_table = _quote_ident(table)
    q_dt = _quote_ident(dt_col)
    q_symbol = _quote_ident("symbol")
    sql = f"""
    WITH ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY {q_symbol} ORDER BY {q_dt} DESC) AS __rn
      FROM {q_table}
      WHERE {q_symbol} IS NOT NULL
        AND TRIM(CAST({q_symbol} AS TEXT)) <> ''
        AND {q_dt} IS NOT NULL
        AND {q_dt} <= ?
    )
    SELECT * FROM ranked
    WHERE __rn <= ?
    ORDER BY {q_symbol} ASC, {q_dt} ASC
    """
    try:
        return pd.read_sql_query(sql, conn, params=(str(latest_dt), int(rows_per_symbol)))
    except Exception:
        logger.exception("[MTF DIFF 1M] history read failed table=%s latest_dt=%s", table, latest_dt)
        return pd.DataFrame()


def _read_1m_after(conn: sqlite3.Connection, *, latest_dt: pd.Timestamp, max_rows: int) -> pd.DataFrame:
    table = _TABLES[1]
    if not _table_exists(conn, table):
        return pd.DataFrame()
    cols = set(_table_columns(conn, table))
    dt_col = _first_existing(cols, ("datetime", "end_time", "time", "start_time"))
    if not dt_col or "symbol" not in cols:
        return pd.DataFrame()
    q_table = _quote_ident(table)
    q_dt = _quote_ident(dt_col)
    sql = f"""
    SELECT *
    FROM {q_table}
    WHERE {q_dt} IS NOT NULL
      AND {q_dt} > ?
    ORDER BY {q_dt} ASC
    LIMIT ?
    """
    try:
        return pd.read_sql_query(sql, conn, params=(str(latest_dt), int(max_rows)))
    except Exception:
        logger.exception("[MTF DIFF 1M] 1m diff read failed latest_dt=%s", latest_dt)
        return pd.DataFrame()


def _ceil_to_interval(ts: pd.Timestamp, interval: int) -> pd.Timestamp:
    # 1分足のdatetimeを、3m/5mの終端時刻へ寄せる。
    # 例: 09:16,09:17,09:18 -> 09:18 for 3m / 09:31..09:35 -> 09:35 for 5m
    base = ts.normalize()
    mins = int((ts - base).total_seconds() // 60)
    end_min = ((mins + int(interval) - 1) // int(interval)) * int(interval)
    return base + pd.Timedelta(minutes=end_min)


def _resample_1m_to_mtf(df_1m: pd.DataFrame, *, interval: int, allow_partial: bool) -> pd.DataFrame:
    src = _normalize_summary_df(df_1m, interval=1)
    if src.empty:
        return pd.DataFrame()
    required = {"symbol", "datetime", "open", "high", "low", "close"}
    if not required.issubset(set(src.columns)):
        logger.warning("[MTF DIFF 1M] 1m source missing required cols missing=%s", sorted(required - set(src.columns)))
        return pd.DataFrame()
    x = src.copy()
    x["__mtf_end"] = x["datetime"].apply(lambda v: _ceil_to_interval(pd.Timestamp(v), interval))
    x = x.sort_values(["symbol", "datetime"], kind="stable")
    rows: list[dict[str, Any]] = []
    for (symbol, end_dt), g in x.groupby(["symbol", "__mtf_end"], sort=True):
        if not allow_partial and len(g) < int(interval):
            continue
        row: dict[str, Any] = {
            "symbol": str(symbol),
            "datetime": pd.Timestamp(end_dt),
            "end_time": pd.Timestamp(end_dt),
            "start_time": pd.Timestamp(end_dt) - pd.Timedelta(minutes=int(interval)),
            "interval": int(interval),
            "source": "push_1m_diff",
            "open": pd.to_numeric(g["open"], errors="coerce").dropna().iloc[0] if pd.to_numeric(g["open"], errors="coerce").notna().any() else pd.NA,
            "high": pd.to_numeric(g["high"], errors="coerce").max(),
            "low": pd.to_numeric(g["low"], errors="coerce").min(),
            "close": pd.to_numeric(g["close"], errors="coerce").dropna().iloc[-1] if pd.to_numeric(g["close"], errors="coerce").notna().any() else pd.NA,
            "__diff_from_1m": True,
            "__diff_1m_rows": int(len(g)),
        }
        if "symbolname" in g.columns:
            vals = g["symbolname"].dropna().astype(str)
            row["symbolname"] = vals.iloc[-1] if len(vals) else str(symbol)
        else:
            row["symbolname"] = str(symbol)
        row["open_price"] = row["open"]
        row["high_price"] = row["high"]
        row["low_price"] = row["low"]
        row["close_price"] = row["close"]
        for cands, out_col, how in (
            (("volume", "trading_volume"), "volume", "sum"),
            (("trading_volume", "volume"), "trading_volume", "sum"),
            (("trading_value", "turnover"), "trading_value", "sum"),
            (("turnover", "trading_value"), "turnover", "sum"),
            (("tick_count",), "tick_count", "sum"),
        ):
            val = 0.0
            for c in cands:
                if c in g.columns:
                    val = float(pd.to_numeric(g[c], errors="coerce").fillna(0).sum())
                    break
            row[out_col] = val
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.date
    out["time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.time
    out["time_range"] = pd.to_datetime(out["start_time"], errors="coerce").dt.strftime("%H:%M") + " - " + pd.to_datetime(out["end_time"], errors="coerce").dt.strftime("%H:%M")
    return _normalize_summary_df(out, interval=interval)


def build_incremental_mtf_from_1m(interval: int) -> dict[str, Any]:
    """
    Returns:
      {
        ok: bool,
        reason: str,
        history_df: DataFrame,  # MA75計算用履歴 + 差分バー
        diff_df: DataFrame,     # DB保存すべき新規3m/5mバー
        latest_dt: Timestamp,
      }
    """
    interval = int(interval)
    if interval not in (3, 5):
        return {"ok": False, "reason": "unsupported_interval", "history_df": pd.DataFrame(), "diff_df": pd.DataFrame()}
    if not _env_bool("SUMMARY_MTF_DIFF_FROM_1M_ENABLED", True):
        return {"ok": False, "reason": "disabled", "history_df": pd.DataFrame(), "diff_df": pd.DataFrame()}

    db_path = _summary_db_path()
    if db_path is None or not db_path.exists():
        return {"ok": False, "reason": "summary_db_missing", "path": str(db_path) if db_path else None, "history_df": pd.DataFrame(), "diff_df": pd.DataFrame()}

    target_table = _TABLES[interval]
    hist_rows = max(1, _env_int("SUMMARY_MTF_DIFF_HISTORY_ROWS", 74))
    max_1m_rows = max(1000, _env_int("SUMMARY_MTF_DIFF_MAX_1M_ROWS", 250000))
    allow_partial = _env_bool("SUMMARY_MTF_DIFF_ALLOW_PARTIAL_BAR", False)

    try:
        with sqlite3.connect(str(db_path), timeout=8) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=8000")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass
            latest_dt = _read_latest_dt(conn, target_table)
            if latest_dt is None:
                return {"ok": False, "reason": "no_latest_mtf", "path": str(db_path), "table": target_table, "history_df": pd.DataFrame(), "diff_df": pd.DataFrame()}
            hist_raw = _read_history_before(conn, target_table, latest_dt=latest_dt, rows_per_symbol=hist_rows)
            one_raw = _read_1m_after(conn, latest_dt=latest_dt, max_rows=max_1m_rows)
    except Exception as e:
        logger.exception("[MTF DIFF 1M] build failed interval=%s path=%s", interval, db_path)
        return {"ok": False, "reason": "exception", "error": str(e), "path": str(db_path), "history_df": pd.DataFrame(), "diff_df": pd.DataFrame()}

    hist_df = _normalize_summary_df(hist_raw, interval=interval)
    diff_df = _resample_1m_to_mtf(one_raw, interval=interval, allow_partial=allow_partial)
    if diff_df.empty:
        logger.info(
            "[MTF DIFF 1M] no diff bars interval=%s latest_dt=%s one_raw_rows=%s allow_partial=%s",
            interval,
            latest_dt,
            len(one_raw) if isinstance(one_raw, pd.DataFrame) else 0,
            allow_partial,
        )
        return {
            "ok": False,
            "reason": "no_completed_diff_bars",
            "path": str(db_path),
            "table": target_table,
            "latest_dt": latest_dt,
            "history_df": hist_df,
            "diff_df": pd.DataFrame(),
            "one_raw_rows": len(one_raw) if isinstance(one_raw, pd.DataFrame) else 0,
        }

    combined = pd.concat([hist_df, diff_df], ignore_index=True, sort=False) if not hist_df.empty else diff_df.copy()
    combined = _normalize_summary_df(combined, interval=interval)
    logger.warning(
        "[MTF DIFF 1M] built interval=%s latest_dt=%s hist_rows=%s diff_rows=%s diff_symbols=%s one_raw_rows=%s path=%s",
        interval,
        latest_dt,
        len(hist_df),
        len(diff_df),
        int(diff_df["symbol"].nunique()) if "symbol" in diff_df.columns else 0,
        len(one_raw) if isinstance(one_raw, pd.DataFrame) else 0,
        db_path,
    )
    return {
        "ok": True,
        "reason": "ok",
        "path": str(db_path),
        "table": target_table,
        "latest_dt": latest_dt,
        "history_df": combined,
        "diff_df": diff_df,
        "hist_rows": len(hist_df),
        "diff_rows": len(diff_df),
        "one_raw_rows": len(one_raw) if isinstance(one_raw, pd.DataFrame) else 0,
    }


def extract_diff_rows(df_hist_with_indicators: pd.DataFrame, diff_seed_df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """indicator/scoring後のdf_histから、差分バーだけを抽出する。"""
    hist = _normalize_summary_df(df_hist_with_indicators, interval=int(interval))
    seed = _normalize_summary_df(diff_seed_df, interval=int(interval))
    if hist.empty or seed.empty:
        return pd.DataFrame()
    keys = seed[["symbol", "datetime"]].drop_duplicates().copy()
    out = hist.merge(keys.assign(__keep=True), on=["symbol", "datetime"], how="inner")
    out = out.drop(columns=["__keep"], errors="ignore")
    return _normalize_summary_df(out, interval=int(interval))


__all__ = ["build_incremental_mtf_from_1m", "extract_diff_rows"]
