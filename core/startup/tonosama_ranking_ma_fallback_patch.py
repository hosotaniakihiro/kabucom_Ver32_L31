# ============================================================
# File   : core/startup/tonosama_ranking_ma_fallback_patch.py
# Version: V1.0-TONOSAMA-RANKING-MA-FALLBACK
# ------------------------------------------------------------
# TONOSAMA pending直前のランキングMA判定で ranking_snapshot_1min が空の場合、
# ranking_snapshot_v2 / ranking_raw_v2 / ranking_summary_5min / summary1min へfallbackする。
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
_INSTALLED = False
_ORIG_CALC = None

_SYMBOL_COLS = ["symbol", "Symbol", "code", "銘柄コード", "stock_code"]
_TIME_COLS = ["datetime", "snapshot_time", "received_at", "created_at", "inserted_at", "updated_at", "time", "dt"]
_PRICE_COLS = ["current_price", "price", "CurrentPrice", "close", "close_price", "Close", "last_price", "last", "disp_close"]


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _first_existing(cols: set[str] | list[str], names: list[str]) -> str | None:
    cset = set(cols)
    for n in names:
        if n in cset:
            return n
    return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
        return row is not None
    except Exception:
        return False


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()}
    except Exception:
        return set()


def _resolve_ranking_db_path() -> str | None:
    for k in ("RANKING_DB_PATH", "KABU_RANKING_DB_PATH", "KABUCOM_RANKING_DB_PATH"):
        v = os.getenv(k)
        if v and str(v).strip():
            return str(v).strip()
    try:
        from database.paths.ranking_paths import resolve_ranking_db_path
        p = resolve_ranking_db_path()
        if p:
            return str(p)
    except Exception:
        logger.debug("[TONOSAMA RANKING MA FALLBACK] ranking path resolver failed", exc_info=True)
    return None


def _guess_summary_db_path(ranking_db_path: str | None) -> str | None:
    env = os.getenv("SUMMARY_DB_PATH") or os.getenv("KABU_SUMMARY_DB_PATH") or os.getenv("KABUCOM_SUMMARY_DB_PATH")
    if env and str(env).strip():
        return str(env).strip()
    try:
        today = dt.datetime.now().strftime("%Y%m%d")
        if ranking_db_path:
            p = Path(str(ranking_db_path))
            # .../raw_data/kabu_station/ranking/rankingYYYYMMDD.db -> .../summary/summaryYYYYMMDD.db
            parts = list(p.parts)
            if "ranking" in parts:
                idx = parts.index("ranking")
                base = Path(*parts[:idx]) / "summary" / f"summary{today}.db"
                return str(base)
        return str(Path(r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary") / f"summary{today}.db")
    except Exception:
        return None


def _normalise(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    sym_col = _first_existing(list(x.columns), _SYMBOL_COLS)
    time_col = _first_existing(list(x.columns), _TIME_COLS)
    price_col = _first_existing(list(x.columns), _PRICE_COLS)
    if sym_col is None or time_col is None or price_col is None:
        return pd.DataFrame()
    x["symbol"] = x[sym_col].map(_norm_symbol)
    x["datetime"] = pd.to_datetime(x[time_col], errors="coerce")
    x["price"] = pd.to_numeric(x[price_col], errors="coerce")
    sym = _norm_symbol(symbol)
    x = x[(x["symbol"] == sym) & x["datetime"].notna() & x["price"].notna() & (x["price"] > 0)]
    if x.empty:
        return pd.DataFrame()
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce").dt.floor("min")
    x = x.dropna(subset=["datetime", "price"])
    x = x.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    return x[["symbol", "datetime", "price"]].sort_values(["symbol", "datetime"])


def _load_from_table(conn: sqlite3.Connection, table: str, symbol: str, limit: int) -> pd.DataFrame:
    if not _table_exists(conn, table):
        return pd.DataFrame()
    cols = _table_cols(conn, table)
    sym_col = _first_existing(cols, _SYMBOL_COLS)
    time_col = _first_existing(cols, _TIME_COLS)
    price_col = _first_existing(cols, _PRICE_COLS)
    if sym_col is None or time_col is None or price_col is None:
        logger.debug("[TONOSAMA RANKING MA FALLBACK] skip table=%s missing sym=%s time=%s price=%s cols=%s", table, sym_col, time_col, price_col, sorted(cols))
        return pd.DataFrame()
    q = (
        f"SELECT * FROM {_quote(table)} "
        f"WHERE CAST({_quote(sym_col)} AS TEXT)=? "
        f"ORDER BY {_quote(time_col)} DESC LIMIT ?"
    )
    df = pd.read_sql_query(q, conn, params=(_norm_symbol(symbol), int(limit)))
    return _normalise(df, symbol)


def _fallback_symbol_prices(symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    sym = _norm_symbol(symbol)
    lookback = max(_env_int("TONOSAMA_RANKING_MA_LOOKBACK_ROWS", 30), 10)
    ranking_db = _resolve_ranking_db_path()
    tried: list[dict[str, Any]] = []

    ranking_tables = [
        os.getenv("TONOSAMA_RANKING_MA_PRIMARY_TABLE", "ranking_snapshot_1min"),
        "ranking_snapshot_v2",
        "ranking_raw_v2",
        "ranking_summary_5min",
        "ranking_summary_3min",
        "ranking_summary_1min",
    ]
    ranking_tables = [t for i, t in enumerate(ranking_tables) if t and t not in ranking_tables[:i]]

    if ranking_db:
        try:
            with sqlite3.connect(str(ranking_db), timeout=_env_float("TONOSAMA_RANKING_MA_DB_TIMEOUT", 1.5)) as conn:
                for table in ranking_tables:
                    df = _load_from_table(conn, table, sym, lookback)
                    tried.append({"db": ranking_db, "table": table, "rows": int(len(df)) if isinstance(df, pd.DataFrame) else 0})
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        return df.tail(lookback), {"source": "ranking_db", "db": ranking_db, "table": table, "tried": tried}
        except Exception:
            logger.debug("[TONOSAMA RANKING MA FALLBACK] ranking db fallback failed path=%s symbol=%s", ranking_db, sym, exc_info=True)

    if _env_bool("TONOSAMA_RANKING_MA_ALLOW_SUMMARY_FALLBACK", True):
        summary_db = _guess_summary_db_path(ranking_db)
        summary_tables = ["stock_summary_1min", "summary_1min", "stock_summary"]
        if summary_db:
            try:
                with sqlite3.connect(str(summary_db), timeout=_env_float("TONOSAMA_RANKING_MA_DB_TIMEOUT", 1.5)) as conn:
                    for table in summary_tables:
                        df = _load_from_table(conn, table, sym, lookback)
                        tried.append({"db": summary_db, "table": table, "rows": int(len(df)) if isinstance(df, pd.DataFrame) else 0})
                        if isinstance(df, pd.DataFrame) and not df.empty:
                            return df.tail(lookback), {"source": "summary_db", "db": summary_db, "table": table, "tried": tried}
            except Exception:
                logger.debug("[TONOSAMA RANKING MA FALLBACK] summary db fallback failed path=%s symbol=%s", summary_db, sym, exc_info=True)

    return pd.DataFrame(), {"source": "none", "tried": tried, "ranking_db": ranking_db}


def _calc_from_df(symbol: str, df: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    sym = _norm_symbol(symbol)
    max_age = _env_float("TONOSAMA_RANKING_MA_MAX_AGE_MIN", 30.0)
    x = df.sort_values("datetime").tail(max(_env_int("TONOSAMA_RANKING_MA_LOOKBACK_ROWS", 30), 10)).copy()
    x["ma3"] = x["price"].rolling(3, min_periods=2).mean()
    x["ma5"] = x["price"].rolling(5, min_periods=2).mean()
    x["ma3_prev"] = x["ma3"].shift(1)
    x["ma5_prev"] = x["ma5"].shift(1)
    latest = x.tail(1).iloc[0]
    latest_dt = pd.to_datetime(latest["datetime"], errors="coerce")
    age_min = None
    if pd.notna(latest_dt):
        age_min = (pd.Timestamp(dt.datetime.now()) - latest_dt).total_seconds() / 60.0
    ma3 = float(latest.get("ma3") or 0.0)
    ma5 = float(latest.get("ma5") or 0.0)
    ma3_prev = float(latest.get("ma3_prev") or 0.0)
    ma5_prev = float(latest.get("ma5_prev") or 0.0)
    ma3_slope = ma3 - ma3_prev if ma3_prev else 0.0
    ma5_slope = ma5 - ma5_prev if ma5_prev else 0.0
    result = {
        "ok": True,
        "symbol": sym,
        "source": meta.get("source", "fallback"),
        "table": meta.get("table"),
        "db": meta.get("db"),
        "rows": int(len(x)),
        "latest_dt": str(latest_dt) if pd.notna(latest_dt) else None,
        "age_min": round(float(age_min), 3) if age_min is not None else None,
        "ma3": ma3,
        "ma5": ma5,
        "ma3_prev": ma3_prev,
        "ma5_prev": ma5_prev,
        "ma3_slope": ma3_slope,
        "ma5_slope": ma5_slope,
        "ma3_slope_pct": (ma3_slope / ma3_prev * 100.0) if ma3_prev else 0.0,
        "ma5_slope_pct": (ma5_slope / ma5_prev * 100.0) if ma5_prev else 0.0,
        "reason": "ok_fallback",
        "tried": meta.get("tried", []),
    }
    if age_min is not None and age_min > max_age:
        result["ok"] = False
        result["reason"] = "ranking_ma_fallback_stale"
    return result


def _patched_calc(symbol: str) -> dict[str, Any]:
    try:
        primary = _ORIG_CALC(symbol)  # type: ignore[misc]
        if isinstance(primary, dict) and primary.get("ok"):
            return primary
        if isinstance(primary, dict):
            reason = str(primary.get("reason") or "")
            if reason not in {"ranking_snapshot_empty", "ranking_snapshot_stale", "unknown", "exception"}:
                return primary
        else:
            primary = {"reason": "primary_non_dict"}

        df, meta = _fallback_symbol_prices(symbol)
        if df is None or df.empty:
            out = dict(primary)
            out.update({"ok": False, "reason": "ranking_ma_all_fallback_empty", "fallback": meta})
            logger.warning("[TONOSAMA RANKING MA FALLBACK] empty symbol=%s primary=%s meta=%s", symbol, primary, meta)
            return out

        out = _calc_from_df(symbol, df, meta)
        logger.warning(
            "[TONOSAMA RANKING MA FALLBACK] used symbol=%s source=%s table=%s rows=%s latest=%s age=%s ma3_slope=%.6f ma5_slope=%.6f",
            out.get("symbol"), out.get("source"), out.get("table"), out.get("rows"), out.get("latest_dt"), out.get("age_min"),
            float(out.get("ma3_slope") or 0.0), float(out.get("ma5_slope") or 0.0),
        )
        return out
    except Exception:
        logger.exception("[TONOSAMA RANKING MA FALLBACK] calc failed symbol=%s", symbol)
        return _ORIG_CALC(symbol)  # type: ignore[misc]


def install() -> bool:
    global _INSTALLED, _ORIG_CALC
    if _INSTALLED:
        return True
    try:
        import trading.entry.tonosama.ranking_snapshot_ma_guard as guard

        fn = getattr(guard, "calc_ranking_snapshot_ma", None)
        if not callable(fn):
            logger.warning("[TONOSAMA RANKING MA FALLBACK] target not found")
            return False
        _ORIG_CALC = fn
        guard.calc_ranking_snapshot_ma = _patched_calc
        _INSTALLED = True
        logger.warning("[TONOSAMA RANKING MA FALLBACK] installed v1")
        return True
    except Exception:
        logger.exception("[TONOSAMA RANKING MA FALLBACK] install failed")
        return False
