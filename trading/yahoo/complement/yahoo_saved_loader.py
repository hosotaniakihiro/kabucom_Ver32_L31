from __future__ import annotations
import logging, sqlite3
from pathlib import Path
import pandas as pd
from .constants import DEFAULT_YAHOO_1MIN_DB_TEMPLATE, YAHOO_REFLECT_DELAY_MINUTES
from .logging_utils import log_df_profile
from .time_window import normalize_trade_date_to_yyyymmdd, trade_date_hyphen, resolve_yahoo_reflect_end_dt
logger = logging.getLogger(__name__)

def _resolve_yahoo_db_path(yyyymmdd: str) -> str:
    candidates: list[str] = []
    for mod, fn in (("trading.yahoo.storage.yahoo_db_path", "get_yahoo_1min_db_path"), ("trading.yahoo.storage.yahoo_db_path", "resolve_yahoo_1min_db_path")):
        try:
            m = __import__(mod, fromlist=[fn]); p = getattr(m, fn)(yyyymmdd)
            if p: candidates.append(str(p))
        except Exception: pass
    candidates.append(DEFAULT_YAHOO_1MIN_DB_TEMPLATE.format(yyyymmdd=yyyymmdd))
    for p in candidates:
        if p and Path(p).exists(): return p
    return candidates[-1]

def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    try: return [str(r[1]) for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
    except Exception: return []

def _pick(cols: set[str], *names: str) -> str | None:
    for name in names:
        if name in cols: return name
    return None

def load_saved_yahoo_1min_for_summary(*, trade_date=None, target_date=None, symbols=None, start_dt: str | None = None, end_dt: str | None = None, reason: str = "yahoo-reload") -> pd.DataFrame:
    yyyymmdd = normalize_trade_date_to_yyyymmdd(trade_date or target_date); hyphen = trade_date_hyphen(yyyymmdd)
    if start_dt is None: start_dt = f"{hyphen} 09:00:00"
    if end_dt is None: end_dt = resolve_yahoo_reflect_end_dt(target_date=yyyymmdd, delay_minutes=YAHOO_REFLECT_DELAY_MINUTES)
    db_path = _resolve_yahoo_db_path(yyyymmdd)
    if not Path(db_path).exists():
        logger.warning("[YAHOO COMPLEMENT] reload_saved_yahoo_1min_for_summary skipped db not found path=%s reason=%s", db_path, reason); return pd.DataFrame()
    symbols_list = sorted({str(s).strip() for s in (symbols or []) if str(s).strip()}) if symbols is not None else []
    try:
        with sqlite3.connect(db_path, timeout=30) as con:
            tables = {str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            table = "yahoo_1min" if "yahoo_1min" in tables else next((c for c in ("yahoo_intraday_1min", "intraday_1min", "stock_1min") if c in tables), None)
            if not table:
                logger.warning("[YAHOO COMPLEMENT] reload_saved_yahoo_1min_for_summary skipped table not found db=%s tables=%s", db_path, sorted(tables)); return pd.DataFrame()
            cols = set(_table_columns(con, table)); symbol_col = _pick(cols, "symbol", "code", "ticker"); dt_col = _pick(cols, "datetime", "timestamp", "time", "date_time")
            open_col = _pick(cols, "open", "open_price", "Open"); high_col = _pick(cols, "high", "high_price", "High"); low_col = _pick(cols, "low", "low_price", "Low"); close_col = _pick(cols, "close", "close_price", "Close", "price", "current_price", "adj_close"); volume_col = _pick(cols, "volume", "trading_volume", "Volume")
            if not symbol_col or not dt_col or not close_col:
                logger.error("[YAHOO COMPLEMENT] reload_saved_yahoo_1min_for_summary failed required cols missing table=%s cols=%s", table, sorted(cols)); return pd.DataFrame()
            params: list[object] = [start_dt, end_dt]; where = ""
            if symbols_list:
                where = f' AND "{symbol_col}" IN ({",".join(["?"] * len(symbols_list))})'; params.extend(symbols_list)
            open_expr = f'"{open_col}"' if open_col else f'"{close_col}"'; high_expr = f'"{high_col}"' if high_col else f'"{close_col}"'; low_expr = f'"{low_col}"' if low_col else f'"{close_col}"'; volume_expr = f'"{volume_col}"' if volume_col else '0'
            sql = f'SELECT "{symbol_col}" AS symbol, "{dt_col}" AS datetime, {open_expr} AS open, {high_expr} AS high, {low_expr} AS low, "{close_col}" AS close, {volume_expr} AS volume FROM "{table}" WHERE "{dt_col}" >= ? AND "{dt_col}" < ? {where} ORDER BY "{symbol_col}", "{dt_col}"'
            df = pd.read_sql_query(sql, con, params=params)
    except Exception:
        logger.exception("[YAHOO COMPLEMENT] reload_saved_yahoo_1min_for_summary failed db=%s reason=%s", db_path, reason); return pd.DataFrame()
    if df.empty:
        logger.warning("[YAHOO COMPLEMENT] %s:reload_saved_yahoo_1min_for_summary empty db=%s target_symbols=%s start=%s end=%s", reason, db_path, len(symbols_list), start_dt, end_dt); return df
    df["symbol"] = df["symbol"].astype(str).str.strip(); df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume"): df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["symbol", "datetime", "close"]); df = df[df["symbol"] != ""]
    reflect_end_ts = pd.to_datetime(end_dt, errors="coerce")
    if pd.notna(reflect_end_ts):
        before = len(df); before_symbols = df["symbol"].nunique() if "symbol" in df.columns else 0; df = df[df["datetime"] < reflect_end_ts]
        logger.info("[YAHOO COMPLEMENT] reflect cutoff applied reason=%s target_date=%s reflect_end_dt=%s rows_before=%s rows_after=%s symbols_before=%s symbols_after=%s", reason, yyyymmdd, end_dt, before, len(df), before_symbols, df["symbol"].nunique() if "symbol" in df.columns and not df.empty else 0)
    if df.empty: return df
    df["open"] = df["open"].fillna(df["close"]); df["high"] = df["high"].fillna(df["close"]); df["low"] = df["low"].fillna(df["close"]); df["volume"] = df["volume"].fillna(0); df = df[df["close"] > 0]
    if df.empty: return df
    df["open_price"] = df["open"]; df["high_price"] = df["high"]; df["low_price"] = df["low"]; df["close_price"] = df["close"]; df["date"] = df["datetime"].dt.strftime("%Y-%m-%d"); df["time"] = df["datetime"].dt.strftime("%H:%M:%S"); df["source"] = "summary_recovery_yahoo_1m"
    df = df.sort_values(["symbol", "datetime"]).drop_duplicates(subset=["symbol", "datetime"], keep="last")
    logger.info("[YAHOO COMPLEMENT] %s:reload_saved_yahoo_1min_for_summary rows=%s unique_symbols=%s dt_min=%s dt_max=%s db=%s table=%s reflect_end_dt=%s", reason, len(df), df["symbol"].nunique() if "symbol" in df.columns else 0, df["datetime"].min(), df["datetime"].max(), db_path, table, end_dt)
    log_df_profile(f"{reason}:loaded_saved_yahoo_1min", df); return df
__all__ = ["load_saved_yahoo_1min_for_summary"]
