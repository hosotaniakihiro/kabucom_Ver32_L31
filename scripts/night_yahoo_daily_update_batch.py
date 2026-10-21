# ============================================================
# File   : scripts/night_yahoo_daily_update_batch.py
# Version: V2-NIGHT-YAHOO-DAILY-YF-FALLBACK
# ------------------------------------------------------------
# 夜間にYahoo Financeから全銘柄の日足を取得し、日足テクニカル・
# ローソク足・売買シグナル・ランキング指標を計算して
# daily_db/stock_analysis.db に保存する。
#
# V2:
#   - yfinance取得を ticker文字列 positional 呼び出しに変更
#   - yf.Ticker(...).history() フォールバックを追加
#   - 取得失敗理由をログに出す
#   - 連続NGが多すぎる場合はネットワーク/Yahoo側異常として早期停止
# ============================================================

from __future__ import annotations

import argparse
import concurrent.futures
import importlib
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from trading.yahoo.pipeline.complement.constants import DEFAULT_BASE_DIR
except Exception:
    DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"

LOG = logging.getLogger("night_yahoo_daily_update_batch")
VERSION = "V4-NIGHT-YAHOO-DAILY-INCREMENTAL-FULL-COLUMNS"

DB_DIR_NAME = "daily_db"
DB_FILE_NAME = "stock_analysis.db"
DB_TABLE_HISTORY = "stock_analysis_history"
DB_TABLE_LATEST = "stock_analysis_latest"
RUN_HISTORY_TABLE = "run_history"

REQUIRED_PRICE_COLUMNS = ["open", "high", "low", "close", "volume"]


def _setup_logging() -> None:
    level = os.environ.get("NIGHT_YAHOO_DAILY_LOG_LEVEL", os.environ.get("NIGHT_YAHOO_LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _base_dir() -> Path:
    return Path(os.environ.get("AUTOSTOCK_BASE_DIR") or DEFAULT_BASE_DIR)


def _db_path() -> Path:
    return Path(os.environ.get("NIGHT_YAHOO_DAILY_DB_PATH") or (_base_dir() / DB_DIR_NAME / DB_FILE_NAME))


def _normalize_symbol(value: Any) -> str:
    s = str(value or "").strip().upper()
    if not s or s in {"NAN", "NONE", "NULL", "-"}:
        return ""
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(4) if s.isdigit() and len(s) < 4 else s


def _to_yahoo_ticker(symbol: str) -> str:
    s = _normalize_symbol(symbol)
    return f"{s}.T" if s else ""


def _chunks(seq: list[Any], size: int) -> Iterable[list[Any]]:
    size = max(1, int(size))
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _load_symbols_from_env() -> list[dict[str, str]]:
    raw = os.environ.get("NIGHT_YAHOO_SYMBOLS", "").strip()
    if not raw:
        return []
    out: dict[str, dict[str, str]] = {}
    for x in raw.replace("\n", ",").split(","):
        s = _normalize_symbol(x)
        if s:
            out[s] = {"symbol": s, "symbolname": "", "market": ""}
    return [out[k] for k in sorted(out)]


def _load_symbols_from_symbol_flags_db(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    con = None
    try:
        con = sqlite3.connect(str(path), timeout=10)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        preferred = ["symbol_flags", "symbols", "watchlist"]
        ordered = [t for t in preferred if t in tables] + [t for t in tables if t not in preferred]
        for table in ordered:
            cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
            symbol_col = next((c for c in ["symbol", "code", "銘柄コード", "Symbol"] if c in cols), None)
            if not symbol_col:
                continue
            name_col = next((c for c in ["symbolname", "symbol_name", "name", "銘柄名", "SymbolName"] if c in cols), None)
            market_col = next((c for c in ["market", "市場", "market_name", "ExchangeName"] if c in cols), None)
            select_cols = [symbol_col]
            if name_col:
                select_cols.append(name_col)
            if market_col:
                select_cols.append(market_col)
            rows = con.execute(f"SELECT DISTINCT {', '.join(select_cols)} FROM {table}").fetchall()
            out: dict[str, dict[str, str]] = {}
            for row in rows:
                s = _normalize_symbol(row[0])
                if not s:
                    continue
                out[s] = {
                    "symbol": s,
                    "symbolname": str(row[1] or "") if name_col and len(row) > 1 else "",
                    "market": str(row[2] or "") if market_col and len(row) > 2 else "",
                }
            if out:
                LOG.info("[NIGHT YAHOO DAILY] loaded symbols from db table=%s count=%s", table, len(out))
                return [out[k] for k in sorted(out)]
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY] symbol db load failed path=%s", path, exc_info=True)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    return []


def _load_symbols_from_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, dtype=str, encoding="cp932")
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY] csv load failed path=%s", path, exc_info=True)
        return []
    symbol_col = next((c for c in ["symbol", "code", "コード", "銘柄コード", "Symbol"] if c in df.columns), None)
    if symbol_col is None and len(df.columns) > 0:
        symbol_col = str(df.columns[0])
    name_col = next((c for c in ["symbolname", "symbol_name", "name", "銘柄名", "SymbolName"] if c in df.columns), None)
    market_col = next((c for c in ["market", "市場", "market_name", "ExchangeName"] if c in df.columns), None)
    out: dict[str, dict[str, str]] = {}
    for _, row in df.iterrows():
        s = _normalize_symbol(row.get(symbol_col))
        if not s:
            continue
        out[s] = {
            "symbol": s,
            "symbolname": str(row.get(name_col, "") or "") if name_col else "",
            "market": str(row.get(market_col, "") or "") if market_col else "",
        }
    return [out[k] for k in sorted(out)]


def load_symbol_records() -> list[dict[str, str]]:
    records = _load_symbols_from_env()
    if records:
        return records
    base = _base_dir()
    db_env = os.environ.get("NIGHT_YAHOO_SYMBOL_DB")
    candidates = [Path(db_env)] if db_env else []
    candidates += [base / "Basic" / "symbol_flags.db", base / "basic" / "symbol_flags.db"]
    for p in candidates:
        records = _load_symbols_from_symbol_flags_db(p)
        if records:
            return records
    csv_env = os.environ.get("NIGHT_YAHOO_SYMBOL_CSV")
    csv_candidates = [Path(csv_env)] if csv_env else []
    csv_candidates += [base / "Basic" / "symbols.csv", base / "Basic" / "watchlist.csv", PROJECT_ROOT / "symbols.csv", PROJECT_ROOT / "watchlist.csv"]
    for p in csv_candidates:
        records = _load_symbols_from_csv(p)
        if records:
            LOG.info("[NIGHT YAHOO DAILY] loaded symbols from csv path=%s count=%s", p, len(records))
            return records
    raise RuntimeError("No symbols found for daily update")


def _import_first(names: list[str]):
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _call_if_exists(module_names: list[str], func_name: str, df: pd.DataFrame) -> pd.DataFrame:
    mod = _import_first(module_names)
    if mod is None:
        return df
    fn = getattr(mod, func_name, None)
    if not callable(fn):
        return df
    try:
        return fn(df)
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY] %s.%s failed", getattr(mod, "__name__", mod), func_name, exc_info=True)
        return df


# ============================================================
# PANDAS SAFETY (旧 scripts/night_yahoo_pandas_safety_patch.py)
# ------------------------------------------------------------
# yfinance/pandas の返却形揺れ (MultiIndex / 重複カラム) で
# 全銘柄が連続失敗するのを防ぐ。旧パッチは _standardize_daily と
# _save_symbol_df を外から包んでいたので、その 2 段構成を本文へ写した。
# ============================================================


def _standardize_daily(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    from scripts.night_yahoo_pandas_safety import fallback_standardize_daily, sanitize_columns

    clean = sanitize_columns(raw) if isinstance(raw, pd.DataFrame) else raw
    try:
        return _standardize_daily_core(clean, symbol)
    except Exception as e:
        LOG.warning("[NIGHT YAHOO PANDAS SAFETY] standardize retry symbol=%s err=%s", symbol, e)
        return fallback_standardize_daily(raw, symbol, _normalize_symbol)


def _standardize_daily_core(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index()
    dt_col = next((c for c in df.columns if str(c).lower() in {"date", "datetime", "index"}), df.columns[0])
    df["date"] = pd.to_datetime(df[dt_col], errors="coerce").dt.normalize()
    rename: dict[Any, str] = {}
    for c in df.columns:
        lc = str(c).strip().lower().replace(" ", "_")
        if lc == "open": rename[c] = "open"
        elif lc == "high": rename[c] = "high"
        elif lc == "low": rename[c] = "low"
        elif lc == "close": rename[c] = "close"
        elif lc in {"adj_close", "adjclose"}: rename[c] = "adj_close"
        elif lc == "volume": rename[c] = "volume"
    df = df.rename(columns=rename)
    for c in ["open", "high", "low", "close", "volume"]:
        if c not in df.columns:
            return pd.DataFrame()
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "adj_close" not in df.columns:
        df["adj_close"] = df["close"]
    else:
        df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).copy()
    df["volume"] = df["volume"].fillna(0)
    df["symbol"] = _normalize_symbol(symbol)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last").sort_values("date")
    return df[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]].copy()


# ============================================================
# DIRECT CHART API
# (旧 scripts/night_yahoo_daily_direct_chart_patch.py)
# ------------------------------------------------------------
# sitecustomize の yfinance fail-cache に引っ張られないよう、
# Yahoo chart API を直接叩く経路を先に試し、駄目なら従来の
# yfinance 経路 (_fetch_daily_one_core) へ落とす。
# NIGHT_YAHOO_DIRECT_ONLY=1 のときは chart 失敗で即空を返す。
# 取得ヘルパは scripts/night_yahoo_daily_direct_chart.py に残してある。
# ============================================================


# ============================================================
# STAGE LOG
# (旧 scripts/night_yahoo_daily_stage_log_patch.py)
# ------------------------------------------------------------
# 銘柄ごとに fetch / compute / save のどこで待っているか分かるように
# 各段の begin/done を出す。旧パッチは 3 関数の最外層に入っていたので
# 同じ位置 (公開名) に置き、実装は *_staged へ委譲する。
# ============================================================


def _stage_sym_from_price_df(df: pd.DataFrame) -> str:
    try:
        if df is not None and not df.empty and "symbol" in df.columns:
            return str(df["symbol"].dropna().iloc[0])
    except Exception:
        pass
    return "?"


def _fetch_daily_one(symbol: str, *, period: str, start: Optional[str]) -> pd.DataFrame:
    t0 = time.time()
    LOG.info("[NIGHT YAHOO DAILY STAGE] fetch begin symbol=%s start=%s period=%s", symbol, start or "-", period)
    out = _fetch_daily_one_direct_chart(symbol, period=period, start=start)
    LOG.info("[NIGHT YAHOO DAILY STAGE] fetch done symbol=%s rows=%s elapsed=%.1fs", symbol, 0 if out is None else len(out), time.time() - t0)
    return out


def _fetch_daily_one_direct_chart(symbol: str, *, period: str, start: Optional[str]) -> pd.DataFrame:
    from scripts.night_yahoo_daily_direct_chart import (
        fetch_chart as _dc_fetch_chart,
        normalize_symbol as _dc_normalize_symbol,
        skip_symbols as _dc_skip_symbols,
        timeout_sec as _dc_timeout,
        to_yahoo_ticker as _dc_to_yahoo_ticker,
    )

    norm_symbol = _dc_normalize_symbol(symbol)
    if norm_symbol in _dc_skip_symbols():
        LOG.warning("[NIGHT YAHOO DIRECT CHART] skipped by NIGHT_YAHOO_DAILY_SKIP_SYMBOLS symbol=%s", norm_symbol)
        return pd.DataFrame()
    ticker = _dc_to_yahoo_ticker(norm_symbol)
    errors: list[str] = []
    if ticker:
        try:
            raw = _dc_fetch_chart(ticker, start=start, period=period, timeout=_dc_timeout())
            if raw is not None and not raw.empty:
                raw["symbol"] = norm_symbol
                raw = raw.drop_duplicates(subset=["symbol", "date"], keep="last").sort_values("date")
                LOG.info("[NIGHT YAHOO DIRECT CHART] ok symbol=%s ticker=%s rows=%s", norm_symbol, ticker, len(raw))
                return raw[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]].copy()
            errors.append("chart_empty")
        except Exception as e:
            errors.append(f"chart_error={type(e).__name__}:{e}")
            if str(os.environ.get("NIGHT_YAHOO_DIRECT_ONLY", "0")).strip().lower() in {"1", "true", "yes", "on"}:
                LOG.warning("[NIGHT YAHOO DIRECT CHART] no daily data symbol=%s ticker=%s reason=%s", norm_symbol, ticker, ";".join(errors))
                return pd.DataFrame()

    try:
        out = _fetch_daily_one_core(norm_symbol, period=period, start=start)
        if out is not None and not out.empty:
            return out
        errors.append("original_empty")
    except Exception as e:
        errors.append(f"original_error={type(e).__name__}:{e}")

    LOG.warning("[NIGHT YAHOO DIRECT CHART] no daily data symbol=%s ticker=%s reason=%s", norm_symbol, ticker, ";".join(errors))
    return pd.DataFrame()


def _fetch_daily_one_core(symbol: str, *, period: str, start: Optional[str]) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    ticker = _to_yahoo_ticker(symbol)
    if not ticker:
        return pd.DataFrame()

    errors: list[str] = []

    # 1) yfinance標準の単一ticker指定。tickers= ではなく positional にする。
    try:
        kwargs: dict[str, Any] = {
            "interval": "1d",
            "auto_adjust": False,
            "progress": False,
            "threads": False,
        }
        if start:
            kwargs["start"] = start
        else:
            kwargs["period"] = period
        raw = yf.download(ticker, **kwargs)
        out = _standardize_daily(raw, symbol)
        if not out.empty:
            return out
        errors.append("download_empty")
    except Exception as e:
        errors.append(f"download_error={type(e).__name__}:{e}")

    # 2) フォールバック: Ticker.history
    try:
        hist_kwargs: dict[str, Any] = {
            "interval": "1d",
            "auto_adjust": False,
        }
        if start:
            hist_kwargs["start"] = start
        else:
            hist_kwargs["period"] = period
        raw = yf.Ticker(ticker).history(**hist_kwargs)
        out = _standardize_daily(raw, symbol)
        if not out.empty:
            LOG.info("[NIGHT YAHOO DAILY] fallback history ok symbol=%s ticker=%s rows=%s", symbol, ticker, len(out))
            return out
        errors.append("history_empty")
    except Exception as e:
        errors.append(f"history_error={type(e).__name__}:{e}")

    LOG.warning("[NIGHT YAHOO DAILY] no daily data symbol=%s ticker=%s reason=%s", symbol, ticker, ";".join(errors))
    return pd.DataFrame()


def _add_change_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["prev_close"] = out["close"].shift(1)
    out["change_1d_pct"] = (out["close"] / out["prev_close"] - 1) * 100
    out["change_3d_pct"] = (out["close"] / out["close"].shift(3) - 1) * 100
    out["change_5d_pct"] = (out["close"] / out["close"].shift(5) - 1) * 100
    return out


# ============================================================
# FULL COLUMNS
# (旧 scripts/night_yahoo_daily_full_columns_patch.py V4)
# ------------------------------------------------------------
# 保存先テーブルが期待する全列 (EXPECTED_COLS) を必ず埋める。
# 旧パッチは _run_indicator_pipeline / _save_symbol_df / process_symbol の
# 3 つを包んでいたので、その 3 段構成をそのまま本文へ写した。
# 列定義と enrich 本体は scripts/night_yahoo_daily_full_columns.py にある。
# ============================================================


def _fullcol():
    from scripts import night_yahoo_daily_full_columns as _m

    return _m


# ============================================================
# LIGHT COMPUTE
# (旧 scripts/night_yahoo_daily_light_compute_patch.py)
# ------------------------------------------------------------
# 外部の重い日足計算パイプラインを使わず、full columns の
# enrich_daily_columns だけで列を作る。compute で固まる問題の回避用。
# 旧パッチは _run_indicator_pipeline を full columns 版ごと置き換えて
# いたので、NIGHT_YAHOO_DAILY_LIGHT_COMPUTE (既定 1) で切り替える形にした。
# 無効時は従来どおり full columns 版 (_run_indicator_pipeline_full_columns) を通す。
# ============================================================


def _light_compute_enabled() -> bool:
    return str(os.environ.get("NIGHT_YAHOO_DAILY_LIGHT_COMPUTE", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _light_normalize_symbol(value: Any) -> str:
    s = str(value or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(4) if s.isdigit() and len(s) < 4 else s


def _run_indicator_pipeline(price_df: pd.DataFrame, rec: dict[str, str]) -> pd.DataFrame:
    symbol = str((rec or {}).get("symbol") or _stage_sym_from_price_df(price_df))
    t0 = time.time()
    LOG.info("[NIGHT YAHOO DAILY STAGE] compute begin symbol=%s rows=%s", symbol, 0 if price_df is None else len(price_df))
    out = _run_indicator_pipeline_staged(price_df, rec)
    LOG.info("[NIGHT YAHOO DAILY STAGE] compute done symbol=%s rows=%s cols=%s elapsed=%.1fs", symbol, 0 if out is None else len(out), 0 if out is None else len(out.columns), time.time() - t0)
    return out


def _run_indicator_pipeline_staged(price_df: pd.DataFrame, rec: dict[str, str]) -> pd.DataFrame:
    if not _light_compute_enabled():
        return _run_indicator_pipeline_full_columns(price_df, rec)

    t0 = time.time()
    if price_df is None or price_df.empty:
        return pd.DataFrame()
    p = price_df.copy()
    p["date"] = pd.to_datetime(p["date"], errors="coerce")
    p = p.dropna(subset=["date"]).copy()
    symbol = _light_normalize_symbol((rec or {}).get("symbol") or (p["symbol"].iloc[0] if "symbol" in p.columns and not p.empty else ""))
    stock_name = str((rec or {}).get("symbolname") or (rec or {}).get("stock_name") or "")
    market = str((rec or {}).get("market") or "")

    # full_columns側が期待する基本列へ正規化
    p["stock_code"] = symbol
    p["stock_name"] = stock_name
    p["market"] = market
    if "adj_close" not in p.columns and "close" in p.columns:
        p["adj_close"] = p["close"]
    for c in ["open", "high", "low", "close", "adj_close", "volume"]:
        if c in p.columns:
            p[c] = pd.to_numeric(p[c], errors="coerce")
    base_cols = ["stock_code", "stock_name", "market", "date", "open", "high", "low", "close", "adj_close", "volume"]
    p = p[[c for c in base_cols if c in p.columns]].copy()
    out = _fullcol().enrich_daily_columns(p)
    LOG.info(
        "[NIGHT YAHOO DAILY LIGHT COMPUTE] done symbol=%s rows=%s cols=%s elapsed=%.1fs",
        symbol, len(out), len(out.columns), time.time() - t0,
    )
    return out


def _run_indicator_pipeline_full_columns(price_df: pd.DataFrame, rec: dict[str, str]) -> pd.DataFrame:
    try:
        out = _run_indicator_pipeline_core(price_df, rec)
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY FULLCOL] original pipeline failed, using fallback", exc_info=True)
        # 元パイプライン失敗時も最低限の形を作る
        p = price_df.copy()
        p["date"] = pd.to_datetime(p["date"], errors="coerce")
        out = p.copy()
        out.insert(0, "market", rec.get("market", ""))
        out.insert(0, "stock_name", rec.get("symbolname", ""))
        out.insert(0, "stock_code", rec.get("symbol", ""))
    try:
        return _fullcol().enrich_daily_columns(out)
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY FULLCOL] enrich failed", exc_info=True)
        return out


def _run_indicator_pipeline_core(price_df: pd.DataFrame, rec: dict[str, str]) -> pd.DataFrame:
    if price_df is None or price_df.empty:
        return pd.DataFrame()
    df = price_df.copy().sort_values("date")
    df = df.set_index("date")
    df = df[["open", "high", "low", "close", "adj_close", "volume"]]

    df = _call_if_exists(["technical_indicators", "tech_indicators"], "calculate_all_indicators", df)
    df = _call_if_exists(["candlestick_patterns"], "add_candlestick_patterns", df)
    df = _call_if_exists(["buy_signals"], "detect_all_buy_signals", df)
    df = _call_if_exists(["sell_signals"], "detect_all_sell_signals", df)
    df = _add_change_columns(df)

    out = df.reset_index()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out.insert(0, "market", rec.get("market", ""))
    out.insert(0, "stock_name", rec.get("symbolname", ""))
    out.insert(0, "stock_code", rec.get("symbol", ""))

    out = _call_if_exists(["ranking_metrics"], "prepare_ranking_metrics", out)
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out.sort_values("date", ascending=False).reset_index(drop=True)
    return out


def _sqlite_type(s: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(s):
        return "INTEGER"
    if pd.api.types.is_float_dtype(s):
        return "REAL"
    if pd.api.types.is_bool_dtype(s):
        return "INTEGER"
    return "TEXT"


def _ensure_table(conn: sqlite3.Connection, table: str, df: pd.DataFrame, pk_cols: list[str]) -> None:
    cols_sql = []
    for col in df.columns:
        cols_sql.append(f'"{col}" {_sqlite_type(df[col])}')
    pk = ", ".join([f'"{c}"' for c in pk_cols])
    conn.execute(f'CREATE TABLE IF NOT EXISTS {table} ({", ".join(cols_sql)}, PRIMARY KEY ({pk}))')
    existing = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col in df.columns:
        if col not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN "{col}" {_sqlite_type(df[col])}')
    if table == DB_TABLE_HISTORY:
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table} ("date")')
    if table == DB_TABLE_LATEST:
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table} ("date")')


def _normalize_for_sql(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_bool_dtype(out[col]):
            out[col] = out[col].astype("Int64")
        elif pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%Y-%m-%d")
    return out.where(pd.notna(out), None)


def _upsert_df(conn: sqlite3.Connection, table: str, df: pd.DataFrame, pk_cols: list[str]) -> int:
    if df is None or df.empty:
        return 0
    data = _normalize_for_sql(df)
    _ensure_table(conn, table, data, pk_cols)
    cols = list(data.columns)
    quoted = ", ".join([f'"{c}"' for c in cols])
    placeholders = ", ".join(["?"] * len(cols))
    conflict = ", ".join([f'"{c}"' for c in pk_cols])
    updates = ", ".join([f'"{c}"=excluded."{c}"' for c in cols if c not in pk_cols])
    sql = f'INSERT INTO {table} ({quoted}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO UPDATE SET {updates}'
    conn.executemany(sql, data[cols].itertuples(index=False, name=None))
    return len(data)


# ============================================================
# CHUNKED SAVE
# (旧 scripts/night_yahoo_daily_save_chunk_patch.py)
# ------------------------------------------------------------
# NAS/DB ロックで長時間止まらないよう、history を chunk 単位で
# commit しながら保存する。旧パッチは _save_symbol_df を
# **完全に置き換えて** いた (env ゲート無し) ため、
# full columns の save 層と pandas safety の save 層は実運用では
# 到達しない。両者はそれぞれ
#   _save_symbol_df_full_columns / _save_symbol_df_safety
# として残してあるが、通常経路からは呼ばれない。
# ============================================================


def _save_env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _save_env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(default) if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return float(default)


def _save_symbol_df(db_path: Path, df: pd.DataFrame) -> tuple[int, int]:
    symbol = _stage_sym_from_price_df(df)
    try:
        if df is not None and not df.empty and "stock_code" in df.columns:
            symbol = str(df["stock_code"].dropna().iloc[0])
    except Exception:
        pass
    t0 = time.time()
    LOG.info("[NIGHT YAHOO DAILY STAGE] save begin symbol=%s rows=%s cols=%s db=%s", symbol, 0 if df is None else len(df), 0 if df is None else len(df.columns), db_path)
    out = _save_symbol_df_chunked(db_path, df)
    LOG.info("[NIGHT YAHOO DAILY STAGE] save done symbol=%s result=%s elapsed=%.1fs", symbol, out, time.time() - t0)
    return out


def _save_symbol_df_chunked(db_path: Path, df: pd.DataFrame) -> tuple[int, int]:
    if df is None or df.empty:
        return 0, 0
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_size = max(10, _save_env_int("NIGHT_YAHOO_DAILY_SAVE_CHUNK_SIZE", 100))
    timeout = _save_env_float("NIGHT_YAHOO_DAILY_SQLITE_TIMEOUT", 3.0)
    busy_ms = max(500, _save_env_int("NIGHT_YAHOO_DAILY_SQLITE_BUSY_TIMEOUT_MS", 3000))
    t0 = time.time()
    LOG.info(
        "[NIGHT YAHOO DAILY SAVE CHUNK] start rows=%s cols=%s chunk=%s timeout=%.1fs busy_ms=%s db=%s",
        len(df), len(df.columns), chunk_size, timeout, busy_ms, db_path,
    )
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    hist_total = 0
    lat = 0
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        conn.execute("PRAGMA synchronous=NORMAL")

        n = len(df)
        for start in range(0, n, chunk_size):
            end = min(start + chunk_size, n)
            LOG.info(
                "[NIGHT YAHOO DAILY SAVE CHUNK] history chunk %s-%s/%s begin elapsed=%.1fs",
                start + 1, end, n, time.time() - t0,
            )
            part = df.iloc[start:end].copy()
            try:
                hist_total += int(_upsert_df(conn, DB_TABLE_HISTORY, part, ["stock_code", "date"]) or 0)
                conn.commit()
                LOG.info(
                    "[NIGHT YAHOO DAILY SAVE CHUNK] history chunk %s-%s/%s committed elapsed=%.1fs",
                    start + 1, end, n, time.time() - t0,
                )
            except sqlite3.OperationalError as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                LOG.warning(
                    "[NIGHT YAHOO DAILY SAVE CHUNK] history chunk %s-%s/%s skipped sqlite error=%s elapsed=%.1fs",
                    start + 1, end, n, e, time.time() - t0,
                )
                # NAS/DB lockで長時間止めない。次chunkへ進む。
                continue

        LOG.info("[NIGHT YAHOO DAILY SAVE CHUNK] latest begin elapsed=%.1fs", time.time() - t0)
        latest = df.copy()
        latest["date"] = pd.to_datetime(latest["date"], errors="coerce")
        latest = latest.dropna(subset=["date"]).sort_values("date").tail(1)
        latest["date"] = latest["date"].dt.strftime("%Y-%m-%d")
        try:
            lat = int(_upsert_df(conn, DB_TABLE_LATEST, latest, ["stock_code"]) or 0)
            conn.commit()
        except sqlite3.OperationalError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            LOG.warning("[NIGHT YAHOO DAILY SAVE CHUNK] latest skipped sqlite error=%s elapsed=%.1fs", e, time.time() - t0)
            lat = 0
        LOG.info(
            "[NIGHT YAHOO DAILY SAVE CHUNK] done history=%s latest=%s elapsed=%.1fs",
            hist_total, lat, time.time() - t0,
        )
        return hist_total, lat
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _save_symbol_df_full_columns(db_path: Path, df: pd.DataFrame) -> tuple[int, int]:
    """旧 full_columns 版の save (現行経路では chunk 版に置き換わって未使用)。"""
    fc = _fullcol()
    if df is not None and not df.empty:
        df = fc.enrich_daily_columns(df)
    result = _save_symbol_df_safety(db_path, df)
    try:
        with sqlite3.connect(str(db_path), timeout=20) as conn:
            fc.ensure_columns(conn, DB_TABLE_HISTORY, fc.EXPECTED_COLS, df)
            fc.ensure_columns(conn, DB_TABLE_LATEST, fc.EXPECTED_COLS, df)
            conn.commit()
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY FULLCOL] schema ensure failed db=%s", db_path, exc_info=True)
    return result


def _save_symbol_df_safety(db_path: Path, df: pd.DataFrame) -> tuple[int, int]:
    from scripts.night_yahoo_pandas_safety import sanitize_columns

    return _save_symbol_df_core(db_path, sanitize_columns(df) if isinstance(df, pd.DataFrame) else df)


def _save_symbol_df_core(db_path: Path, df: pd.DataFrame) -> tuple[int, int]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=float(os.environ.get("NIGHT_YAHOO_DAILY_SQLITE_TIMEOUT", "60")))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        hist = _upsert_df(conn, DB_TABLE_HISTORY, df, ["stock_code", "date"])
        latest = df.copy()
        latest["date"] = pd.to_datetime(latest["date"], errors="coerce")
        latest = latest.dropna(subset=["date"]).sort_values("date").tail(1)
        latest["date"] = latest["date"].dt.strftime("%Y-%m-%d")
        lat = _upsert_df(conn, DB_TABLE_LATEST, latest, ["stock_code"])
        conn.commit()
        return hist, lat
    finally:
        conn.close()


def _record_run(db_path: Path, *, started_at: float, processed: int, failed: int, note: str) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {RUN_HISTORY_TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL, target_date TEXT, processed_count INTEGER, failed_count INTEGER, db_path TEXT, note TEXT)"
        )
        conn.execute(
            f"INSERT INTO {RUN_HISTORY_TABLE} (run_at, target_date, processed_count, failed_count, db_path, note) VALUES (?, ?, ?, ?, ?, ?)",
            (pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), "latest", int(processed), int(failed), str(db_path), note),
        )
        conn.commit()
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY] run_history insert failed", exc_info=True)
    finally:
        conn.close()


# ============================================================
# INCREMENTAL UPDATE
# (旧 scripts/night_yahoo_daily_incremental_patch.py V5-END-DATE)
# ------------------------------------------------------------
# DB の最新日以降だけを取得・計算・保存する差分更新。
# 旧パッチは process_symbol を丸ごと置き換えていた
# (元実装は一度も呼ばれない)。その構造をそのまま本文へ写し、
# 元の全期間リロード版は _process_symbol_full_reload として残してある。
# ヘルパは scripts/night_yahoo_daily_incremental.py に置いた。
# ============================================================


def _incr_helpers():
    from scripts import night_yahoo_daily_incremental as _m

    return _m


def _incr_date_str(v):
    return _incr_helpers().date_str(v)


def _incr_normalize_symbol(v):
    return _incr_helpers().normalize_symbol(v)


def _incr_parse_end_date():
    return _incr_helpers().parse_end_date()


def _incr_merge_prices(warmup, new_rows, symbol):
    return _incr_helpers().merge_prices(warmup, new_rows, symbol)


def _incr_get_latest_date(db_path, symbol, mod):
    return _incr_helpers().get_latest_date(db_path, symbol, mod)


def _incr_load_warmup_prices(db_path, symbol, mod, rows):
    return _incr_helpers().load_warmup_prices(db_path, symbol, mod, rows)


# ============================================================
# SYMBOL TIMEOUT
# (旧 scripts/night_yahoo_daily_timeout_patch.py)
# ------------------------------------------------------------
# 1 銘柄が固まってもバッチ全体を止めないよう、専用スレッドプールで
# 実行して NIGHT_YAHOO_DAILY_SYMBOL_TIMEOUT_SEC (既定 90) で打ち切る。
# 旧パッチは process_symbol の最外層に入っていたので同じ位置に置いた。
# ============================================================

_SYMBOL_TIMEOUT_EXECUTOR: Optional["concurrent.futures.ThreadPoolExecutor"] = None


def _symbol_timeout_sec() -> float:
    try:
        return float(os.environ.get("NIGHT_YAHOO_DAILY_SYMBOL_TIMEOUT_SEC", "90"))
    except Exception:
        return 90.0


def _symbol_timeout_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _SYMBOL_TIMEOUT_EXECUTOR
    if _SYMBOL_TIMEOUT_EXECUTOR is None:
        workers = int(float(os.environ.get("NIGHT_YAHOO_DAILY_TIMEOUT_WORKERS", "4")))
        workers = max(1, min(workers, 16))
        _SYMBOL_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="night-yahoo-daily"
        )
    return _SYMBOL_TIMEOUT_EXECUTOR


def process_symbol(rec: dict[str, str], *, period: str, start: Optional[str], db_path: Path):
    symbol = str((rec or {}).get("symbol", ""))
    timeout = _symbol_timeout_sec()
    fut = _symbol_timeout_executor().submit(_process_symbol_full_columns, rec, period=period, start=start, db_path=db_path)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        try:
            fut.cancel()
        except Exception:
            pass
        LOG.warning(
            "[NIGHT YAHOO DAILY TIMEOUT] symbol timeout symbol=%s timeout=%.1fs -> skip",
            symbol,
            timeout,
        )
        return symbol, False, f"symbol timeout {timeout:.1f}s", 0


def _process_symbol_full_columns(rec: dict[str, str], *, period: str, start: Optional[str], db_path: Path):
    """full columns の repair 層 -> 差分更新層 の順。"""
    symbol, success, msg, nrows = _process_symbol_incremental(rec, period=period, start=start, db_path=db_path)
    repair_enabled = str(os.environ.get("NIGHT_YAHOO_DAILY_FULLCOL_REPAIR", "1")).strip().lower() not in {"0", "false", "no", "off"}
    if repair_enabled and success and int(nrows or 0) == 0:
        sym = str(symbol or rec.get("symbol", "")).strip()
        fc = _fullcol()
        if sym and fc.needs_latest_repair(Path(db_path), sym, sys.modules[__name__]):
            try:
                repaired = fc.repair_recent_rows(Path(db_path), sym, rec, sys.modules[__name__])
                if repaired > 0:
                    return symbol, True, f"{msg}; full-columns repaired rows={repaired}", repaired
            except Exception:
                LOG.warning("[NIGHT YAHOO DAILY FULLCOL] repair failed symbol=%s", sym, exc_info=True)
    return symbol, success, msg, nrows


def _process_symbol_incremental(rec: dict[str, str], *, period: str, start: Optional[str], db_path: Path):
    symbol = _incr_normalize_symbol(rec.get("symbol", ""))
    if not symbol:
        return symbol, False, "empty symbol", 0

    force_full = str(os.environ.get("NIGHT_YAHOO_DAILY_FORCE_FULL", "0")).strip().lower() in {"1", "true", "yes", "on"}
    warmup_rows = int(float(os.environ.get("NIGHT_YAHOO_DAILY_WARMUP_ROWS", "320")))
    end_dt = _incr_parse_end_date()

    # 旧 incremental patch では daily_mod (= このモジュール) を渡していた。
    # 本文化の際に引数の書き換えが漏れて未定義参照になっていたので合わせる。
    latest_dt = None if force_full else _incr_get_latest_date(Path(db_path), symbol, sys.modules[__name__])

    # すでにDB最新日が上限日以上なら、保存対象なし。
    if latest_dt is not None and end_dt is not None and latest_dt >= end_dt:
        return symbol, True, f"up-to-date latest={_incr_date_str(latest_dt)} end={_incr_date_str(end_dt)}", 0

    # 明示 start がある場合は従来どおりその日から。なければDB最新日の翌日から差分取得。
    effective_start = start
    if latest_dt is not None and not effective_start:
        effective_start = _incr_date_str(latest_dt + pd.Timedelta(days=1))

    # 開始日が上限日を超える場合は取得不要。
    if effective_start and end_dt is not None:
        try:
            if pd.Timestamp(effective_start).normalize() > end_dt:
                base = _incr_date_str(latest_dt) if latest_dt is not None else "-"
                return symbol, True, f"up-to-date latest={base} end={_incr_date_str(end_dt)}", 0
        except Exception:
            pass

    try:
        raw_new = _fetch_daily_one(symbol, period=period, start=effective_start)
        if raw_new is None or raw_new.empty:
            if latest_dt is not None:
                return symbol, True, f"up-to-date latest={_incr_date_str(latest_dt)}" + (f" end={_incr_date_str(end_dt)}" if end_dt is not None else ""), 0
            return symbol, False, "no daily data", 0

        raw_new["date"] = pd.to_datetime(raw_new["date"], errors="coerce").dt.normalize()
        raw_new = raw_new.dropna(subset=["date"])

        if end_dt is not None:
            raw_new = raw_new[raw_new["date"] <= end_dt].copy()
            if raw_new.empty:
                base = _incr_date_str(latest_dt) if latest_dt is not None else "-"
                return symbol, True, f"up-to-date latest={base} end={_incr_date_str(end_dt)}", 0

        if latest_dt is not None:
            raw_new = raw_new[raw_new["date"] > latest_dt].copy()
            if raw_new.empty:
                return symbol, True, f"up-to-date latest={_incr_date_str(latest_dt)}" + (f" end={_incr_date_str(end_dt)}" if end_dt is not None else ""), 0

        warmup = pd.DataFrame()
        if latest_dt is not None:
            warmup = _incr_load_warmup_prices(Path(db_path), symbol, sys.modules[__name__], warmup_rows)

        calc_prices = _incr_merge_prices(warmup, raw_new, symbol)
        if end_dt is not None and not calc_prices.empty:
            calc_prices = calc_prices[pd.to_datetime(calc_prices["date"], errors="coerce").dt.normalize() <= end_dt].copy()
        if calc_prices.empty:
            return symbol, False, "no calc prices", 0

        computed = _run_indicator_pipeline(calc_prices, rec)
        if computed.empty:
            return symbol, False, "computed empty", 0

        computed["date"] = pd.to_datetime(computed["date"], errors="coerce")
        computed = computed.dropna(subset=["date"])
        if end_dt is not None:
            computed = computed[computed["date"].dt.normalize() <= end_dt].copy()

        if latest_dt is not None:
            save_df = computed[computed["date"].dt.normalize() > latest_dt].copy()
        else:
            save_df = computed.copy()

        if save_df.empty:
            base = _incr_date_str(latest_dt) if latest_dt is not None else "-"
            return symbol, True, f"up-to-date latest={base}" + (f" end={_incr_date_str(end_dt)}" if end_dt is not None else ""), 0

        save_df["date"] = save_df["date"].dt.strftime("%Y-%m-%d")
        hist, lat = _save_symbol_df(Path(db_path), save_df)
        new_min = pd.to_datetime(save_df["date"], errors="coerce").min()
        new_max = pd.to_datetime(save_df["date"], errors="coerce").max()
        return symbol, True, f"incremental saved new_rows={hist} latest={lat} range={_incr_date_str(new_min)}..{_incr_date_str(new_max)} db_latest_before={_incr_date_str(latest_dt) if latest_dt is not None else '-'}" + (f" end={_incr_date_str(end_dt)}" if end_dt is not None else ""), hist
    except Exception as e:
        LOG.warning("[NIGHT YAHOO DAILY INCR] symbol failed symbol=%s err=%s", symbol, e, exc_info=True)
        return symbol, False, str(e), 0


# 旧パッチが立てていた互換フラグ (外部からの二重 install ガード用)
_NIGHT_YAHOO_DAILY_INCREMENTAL_PATCHED = True
_NIGHT_YAHOO_DAILY_FULL_COLUMNS_PATCHED = True
_NIGHT_YAHOO_DAILY_LIGHT_COMPUTE_PATCHED = True
_NIGHT_YAHOO_DAILY_CHUNK_SAVE_PATCHED = True
_NIGHT_YAHOO_DAILY_STAGE_LOG_PATCHED = True
_NIGHT_YAHOO_DAILY_TIMEOUT_PATCHED = True


def _process_symbol_full_reload(rec: dict[str, str], *, period: str, start: Optional[str], db_path: Path) -> tuple[str, bool, str, int]:
    symbol = rec.get("symbol", "")
    try:
        raw = _fetch_daily_one(symbol, period=period, start=start)
        if raw.empty:
            return symbol, False, "no daily data", 0
        computed = _run_indicator_pipeline(raw, rec)
        if computed.empty:
            return symbol, False, "computed empty", 0
        hist, lat = _save_symbol_df(db_path, computed)
        return symbol, True, f"saved history={hist} latest={lat}", hist
    except Exception as e:
        LOG.warning("[NIGHT YAHOO DAILY] symbol failed symbol=%s err=%s", symbol, e, exc_info=True)
        return symbol, False, str(e), 0


def main(argv: Optional[list[str]] = None) -> int:
    _setup_logging()

    # この環境では Norton の SSL/TLS スキャンが HTTPS を傍受しており、
    # そのルートCAが certifi に無いため Yahoo へ到達できない
    # (CertificateVerifyError / "possibly delisted; no price data found")。
    # 対処は scripts/yahoo_ca_bundle.install() だが、これまで
    # TaskSchedule のラッパー main.py にしか入っておらず、
    # このバッチを直接起動すると必ず 0 件で終わっていた。
    # どちらの入口から来ても効くように、ここでも入れる (install は冪等)。
    try:
        from scripts.yahoo_ca_bundle import install as _install_ca_bundle
        _install_ca_bundle()
    except Exception:
        LOG.exception("[NIGHT YAHOO DAILY] CA bundle install failed; continue")

    parser = argparse.ArgumentParser(description="Nightly Yahoo daily data update batch")
    parser.add_argument("--period", default=os.environ.get("NIGHT_YAHOO_DAILY_PERIOD", "3y"), help="yfinance period when --start is omitted")
    parser.add_argument("--start", default=os.environ.get("NIGHT_YAHOO_DAILY_START", ""), help="optional yfinance start date YYYY-MM-DD")
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("NIGHT_YAHOO_DAILY_BATCH_SIZE", "1")), help="reserved; per-symbol safer by default")
    parser.add_argument("--pause-sec", type=float, default=float(os.environ.get("NIGHT_YAHOO_DAILY_PAUSE_SEC", "0.05")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("NIGHT_YAHOO_DAILY_LIMIT", "0")))
    parser.add_argument("--max-consecutive-fail", type=int, default=int(os.environ.get("NIGHT_YAHOO_DAILY_MAX_CONSECUTIVE_FAIL", "60")))
    args = parser.parse_args(argv)

    db_path = _db_path()
    records = load_symbol_records()
    if args.limit and args.limit > 0:
        records = records[: int(args.limit)]

    LOG.warning("[NIGHT YAHOO DAILY] START version=%s symbols=%s db=%s period=%s start=%s", VERSION, len(records), db_path, args.period, args.start or "-")
    started = time.time()
    ok = 0
    failed = 0
    rows = 0
    total = len(records)
    start_arg = args.start.strip() or None
    consecutive_fail = 0

    for i, rec in enumerate(records, start=1):
        symbol, success, msg, nrows = process_symbol(rec, period=args.period, start=start_arg, db_path=db_path)
        rows += int(nrows or 0)
        if success:
            ok += 1
            consecutive_fail = 0
            LOG.info("[NIGHT YAHOO DAILY] [%s/%s] OK symbol=%s %s", i, total, symbol, msg)
        else:
            failed += 1
            consecutive_fail += 1
            LOG.warning("[NIGHT YAHOO DAILY] [%s/%s] NG symbol=%s %s", i, total, symbol, msg)
            if ok == 0 and consecutive_fail >= int(args.max_consecutive_fail):
                LOG.error(
                    "[NIGHT YAHOO DAILY] abort: first %s symbols all failed. network/yfinance/yahoo may be unavailable. last_symbol=%s",
                    consecutive_fail,
                    symbol,
                )
                break
        if args.pause_sec > 0:
            time.sleep(float(args.pause_sec))

    elapsed = time.time() - started
    note = f"version={VERSION} ok={ok} failed={failed} rows={rows} elapsed={elapsed:.1f}s"
    _record_run(db_path, started_at=started, processed=ok, failed=failed, note=note)
    LOG.warning("[NIGHT YAHOO DAILY] DONE ok=%s failed=%s rows=%s elapsed=%.1fs db=%s", ok, failed, rows, elapsed, db_path)
    return 0 if ok > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
