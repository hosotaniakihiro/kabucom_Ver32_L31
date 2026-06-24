# ============================================================
# File   : scripts/night_yahoo_daily_update_batch.py
# Version: V1-NIGHT-YAHOO-DAILY-UPDATE-BATCH
# ------------------------------------------------------------
# 【概要】
#   夜間にYahoo Financeから全銘柄の日足を取得し、日足テクニカル・
#   ローソク足・売買シグナル・ランキング指標を計算して
#   daily_db/stock_analysis.db に保存する。
#
# 【目的】
#   翌営業日の起動時点で、日足MTF/日足フィルターに使う最新日足データを
#   あらかじめ揃える。
# ============================================================

from __future__ import annotations

import argparse
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
VERSION = "V1-NIGHT-YAHOO-DAILY-UPDATE-BATCH"

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
            sql = f"SELECT DISTINCT {', '.join(select_cols)} FROM {table}"
            rows = con.execute(sql).fetchall()
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


def _standardize_daily(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
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


def _fetch_daily_one(symbol: str, *, period: str, start: Optional[str]) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    ticker = _to_yahoo_ticker(symbol)
    if not ticker:
        return pd.DataFrame()
    kwargs: dict[str, Any] = {
        "tickers": ticker,
        "interval": "1d",
        "auto_adjust": False,
        "progress": False,
        "threads": False,
    }
    if start:
        kwargs["start"] = start
    else:
        kwargs["period"] = period
    raw = yf.download(**kwargs)
    return _standardize_daily(raw, symbol)


def _add_change_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["prev_close"] = out["close"].shift(1)
    out["change_1d_pct"] = (out["close"] / out["prev_close"] - 1) * 100
    out["change_3d_pct"] = (out["close"] / out["close"].shift(3) - 1) * 100
    out["change_5d_pct"] = (out["close"] / out["close"].shift(5) - 1) * 100
    return out


def _run_indicator_pipeline(price_df: pd.DataFrame, rec: dict[str, str]) -> pd.DataFrame:
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
        col_type = _sqlite_type(df[col])
        cols_sql.append(f'"{col}" {col_type}')
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


def _save_symbol_df(db_path: Path, df: pd.DataFrame) -> tuple[int, int]:
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


def process_symbol(rec: dict[str, str], *, period: str, start: Optional[str], db_path: Path) -> tuple[str, bool, str, int]:
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
    parser = argparse.ArgumentParser(description="Nightly Yahoo daily data update batch")
    parser.add_argument("--period", default=os.environ.get("NIGHT_YAHOO_DAILY_PERIOD", "3y"), help="yfinance period when --start is omitted")
    parser.add_argument("--start", default=os.environ.get("NIGHT_YAHOO_DAILY_START", ""), help="optional yfinance start date YYYY-MM-DD")
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("NIGHT_YAHOO_DAILY_BATCH_SIZE", "1")), help="reserved; per-symbol safer by default")
    parser.add_argument("--pause-sec", type=float, default=float(os.environ.get("NIGHT_YAHOO_DAILY_PAUSE_SEC", "0.05")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("NIGHT_YAHOO_DAILY_LIMIT", "0")))
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

    for i, rec in enumerate(records, start=1):
        symbol, success, msg, nrows = process_symbol(rec, period=args.period, start=start_arg, db_path=db_path)
        rows += int(nrows or 0)
        if success:
            ok += 1
            LOG.info("[NIGHT YAHOO DAILY] [%s/%s] OK symbol=%s %s", i, total, symbol, msg)
        else:
            failed += 1
            LOG.warning("[NIGHT YAHOO DAILY] [%s/%s] NG symbol=%s %s", i, total, symbol, msg)
        if args.pause_sec > 0:
            time.sleep(float(args.pause_sec))

    elapsed = time.time() - started
    note = f"version={VERSION} ok={ok} failed={failed} rows={rows} elapsed={elapsed:.1f}s"
    _record_run(db_path, started_at=started, processed=ok, failed=failed, note=note)
    LOG.warning("[NIGHT YAHOO DAILY] DONE ok=%s failed=%s rows=%s elapsed=%.1fs db=%s", ok, failed, rows, elapsed, db_path)
    return 0 if ok > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
