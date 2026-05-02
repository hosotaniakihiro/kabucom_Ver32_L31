# ============================================================
# File   : trading/yahoo/yahoo_summary_bridge.py
# Version: PRODUCTION-STABLE-REV1.0
# Purpose:
#   Yahoo 1分足DBから stock_summary_1min/3min/5min を作成・保存するブリッジ
#
# Important:
#   - Yahoo由来は本物OHLCとして扱う
#   - ranking_summary には保存しない
#   - 保存先は stock_summary_1min / 3min / 5min
#   - ATR / slope_atr_scaled を計算してから scoring → upsert
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Optional imports
# ============================================================

try:
    from trading.summary.indicators.atr_slope_safe import add_atr_and_slope_safe
except Exception:
    add_atr_and_slope_safe = None

try:
    from trading.summary.persistence.safe_upsert import upsert_stock_summary
except Exception:
    upsert_stock_summary = None

try:
    from trading.summary.indicator_calculator import add_all_indicators
except Exception:
    add_all_indicators = None

try:
    from trading.summary.scoring.scoring_main import run_scoring_pipeline
except Exception:
    run_scoring_pipeline = None


# ============================================================
# Constants
# ============================================================

YAHOO_TABLE_CANDIDATES = [
    "yahoo_1min",
    "yahoo_intraday_1min",
    "intraday_1min",
]

SUMMARY_INTERVALS = (1, 3, 5)


# ============================================================
# DB helpers
# ============================================================

def _connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table'
          AND name=?
        LIMIT 1
        """,
        (table,),
    ).fetchone()
    return row is not None


def resolve_yahoo_table(
    con: sqlite3.Connection,
    table: str | None = None,
) -> str:
    if table and _table_exists(con, table):
        return table

    for t in YAHOO_TABLE_CANDIDATES:
        if _table_exists(con, t):
            return t

    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()

    raise RuntimeError(
        f"Yahoo 1min table not found. candidates={YAHOO_TABLE_CANDIDATES} "
        f"tables={[r[0] for r in rows]}"
    )


def _get_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


# ============================================================
# Normalize
# ============================================================

def _first_existing(df: pd.DataFrame, names: Iterable[str]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def normalize_yahoo_1min_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Yahoo 1分足を summary 用の標準OHLCV形式へ変換する。
    """

    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    rename_map = {}

    symbol_col = _first_existing(out, ["symbol", "code", "ticker"])
    datetime_col = _first_existing(out, ["datetime", "Datetime", "timestamp", "time"])
    open_col = _first_existing(out, ["open", "Open", "open_price"])
    high_col = _first_existing(out, ["high", "High", "high_price"])
    low_col = _first_existing(out, ["low", "Low", "low_price"])
    close_col = _first_existing(out, ["close", "Close", "close_price", "price"])
    volume_col = _first_existing(out, ["volume", "Volume", "出来高"])

    if symbol_col and symbol_col != "symbol":
        rename_map[symbol_col] = "symbol"
    if datetime_col and datetime_col != "datetime":
        rename_map[datetime_col] = "datetime"
    if open_col and open_col != "open":
        rename_map[open_col] = "open"
    if high_col and high_col != "high":
        rename_map[high_col] = "high"
    if low_col and low_col != "low":
        rename_map[low_col] = "low"
    if close_col and close_col != "close":
        rename_map[close_col] = "close"
    if volume_col and volume_col != "volume":
        rename_map[volume_col] = "volume"

    out = out.rename(columns=rename_map)

    required = ["symbol", "datetime", "open", "high", "low", "close"]
    missing = [c for c in required if c not in out.columns]
    if missing:
        raise ValueError(f"Yahoo df missing required columns: {missing}")

    out["symbol"] = out["symbol"].astype(str).str.replace(".T", "", regex=False).str.strip()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")

    for c in ["open", "high", "low", "close", "volume"]:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce")

    out = out.dropna(subset=["symbol", "datetime", "open", "high", "low", "close"])
    out = out[out["symbol"] != ""]

    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")
    out["source"] = "yahoo"

    # 互換カラム
    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["close_price"] = out["close"]

    if "symbolname" not in out.columns:
        out["symbolname"] = ""

    out = out.sort_values(["symbol", "datetime"])
    out = out.drop_duplicates(["symbol", "datetime"], keep="last")

    return out


# ============================================================
# Load Yahoo
# ============================================================

def load_yahoo_1min_df(
    yahoo_db_path: str | Path,
    *,
    table: str | None = None,
    symbols: list[str] | None = None,
    start_dt: dt.datetime | str | None = None,
    end_dt: dt.datetime | str | None = None,
) -> pd.DataFrame:
    path = Path(yahoo_db_path)
    if not path.exists():
        raise FileNotFoundError(f"Yahoo DB not found: {path}")

    con = _connect(path)
    try:
        table_name = resolve_yahoo_table(con, table)
        cols = _get_columns(con, table_name)

        datetime_col = "datetime" if "datetime" in cols else None
        if datetime_col is None:
            for c in ["Datetime", "timestamp", "time"]:
                if c in cols:
                    datetime_col = c
                    break

        if datetime_col is None:
            raise RuntimeError(f"datetime column not found table={table_name}")

        sql = f"SELECT * FROM {table_name}"
        where = []
        params = []

        if start_dt is not None:
            where.append(f"{datetime_col} >= ?")
            params.append(str(pd.to_datetime(start_dt).strftime("%Y-%m-%d %H:%M:%S")))

        if end_dt is not None:
            where.append(f"{datetime_col} <= ?")
            params.append(str(pd.to_datetime(end_dt).strftime("%Y-%m-%d %H:%M:%S")))

        if symbols:
            symbol_col = "symbol" if "symbol" in cols else ("ticker" if "ticker" in cols else None)
            if symbol_col:
                clean_symbols = [str(s).replace(".T", "").strip() for s in symbols]
                ph = ",".join(["?"] * len(clean_symbols))
                where.append(f"REPLACE({symbol_col}, '.T', '') IN ({ph})")
                params.extend(clean_symbols)

        if where:
            sql += " WHERE " + " AND ".join(where)

        sql += f" ORDER BY {datetime_col}"

        df = pd.read_sql_query(sql, con, params=params)
        return normalize_yahoo_1min_df(df)

    finally:
        con.close()


# ============================================================
# Resample
# ============================================================

def resample_ohlcv(
    df_1min: pd.DataFrame,
    *,
    interval: int,
) -> pd.DataFrame:
    if df_1min is None or df_1min.empty:
        return pd.DataFrame()

    if int(interval) == 1:
        return df_1min.copy()

    rule = f"{int(interval)}min"
    rows = []

    base = df_1min.copy()
    base["datetime"] = pd.to_datetime(base["datetime"], errors="coerce")
    base = base.dropna(subset=["datetime"])
    base = base.sort_values(["symbol", "datetime"])

    for symbol, g in base.groupby("symbol", sort=False):
        g = g.set_index("datetime").sort_index()

        agg = g.resample(rule, label="right", closed="right").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )

        agg = agg.dropna(subset=["open", "high", "low", "close"])
        agg["symbol"] = symbol

        symbolname = ""
        if "symbolname" in g.columns and not g["symbolname"].dropna().empty:
            symbolname = str(g["symbolname"].dropna().iloc[-1])

        agg["symbolname"] = symbolname
        rows.append(agg.reset_index())

    if not rows:
        return pd.DataFrame()

    out = pd.concat(rows, ignore_index=True)

    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")
    out["source"] = f"yahoo_resample_{interval}m"

    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["close_price"] = out["close"]

    return out.sort_values(["symbol", "datetime"]).reset_index(drop=True)


# ============================================================
# Indicators / scoring
# ============================================================

def enrich_yahoo_summary_df(
    df: pd.DataFrame,
    *,
    interval: int,
    run_indicators: bool = True,
    run_atr_slope: bool = True,
    run_scoring: bool = True,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if run_indicators and add_all_indicators is not None:
        try:
            out = add_all_indicators(out)
        except TypeError:
            out = add_all_indicators(out, interval=interval)
        except Exception:
            logger.exception("[YAHOO SUMMARY BRIDGE] add_all_indicators failed interval=%s", interval)

    if run_atr_slope and add_atr_and_slope_safe is not None:
        try:
            out = add_atr_and_slope_safe(out)
        except Exception:
            logger.exception("[YAHOO SUMMARY BRIDGE] add_atr_and_slope_safe failed interval=%s", interval)

    if run_scoring and run_scoring_pipeline is not None:
        try:
            out = run_scoring_pipeline(out)
        except TypeError:
            try:
                out = run_scoring_pipeline(out, interval=interval)
            except Exception:
                logger.exception("[YAHOO SUMMARY BRIDGE] run_scoring_pipeline failed interval=%s", interval)
        except Exception:
            logger.exception("[YAHOO SUMMARY BRIDGE] run_scoring_pipeline failed interval=%s", interval)

    out["source"] = f"yahoo_summary_{interval}m"

    return out


# ============================================================
# Save
# ============================================================

def save_yahoo_summary_df(
    df: pd.DataFrame,
    *,
    summary_db_path: str | Path,
    interval: int,
) -> int:
    if df is None or df.empty:
        return 0

    if upsert_stock_summary is None:
        raise ImportError(
            "upsert_stock_summary unavailable. "
            "Check trading.summary.persistence.safe_upsert.py"
        )

    return upsert_stock_summary(
        df,
        db_path=summary_db_path,
        interval=int(interval),
    )


# ============================================================
# Main bridge
# ============================================================

def build_yahoo_summary_from_db(
    *,
    yahoo_db_path: str | Path,
    summary_db_path: str | Path,
    yahoo_table: str | None = None,
    symbols: list[str] | None = None,
    start_dt: dt.datetime | str | None = None,
    end_dt: dt.datetime | str | None = None,
    intervals: Iterable[int] = SUMMARY_INTERVALS,
    save: bool = True,
    run_indicators: bool = True,
    run_atr_slope: bool = True,
    run_scoring: bool = True,
) -> dict[int, pd.DataFrame]:
    """
    Yahoo 1分DBから stock_summary_1min/3min/5min を作成する。

    Returns
    -------
    dict[int, pd.DataFrame]
        interval別のsummary df
    """

    logger.info(
        "[YAHOO SUMMARY BRIDGE] start yahoo_db=%s summary_db=%s intervals=%s symbols=%s",
        yahoo_db_path,
        summary_db_path,
        list(intervals),
        0 if symbols is None else len(symbols),
    )

    df_1min = load_yahoo_1min_df(
        yahoo_db_path,
        table=yahoo_table,
        symbols=symbols,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    if df_1min.empty:
        logger.warning("[YAHOO SUMMARY BRIDGE] yahoo 1min empty")
        return {}

    result: dict[int, pd.DataFrame] = {}

    for interval in intervals:
        interval = int(interval)

        if interval not in SUMMARY_INTERVALS:
            logger.warning("[YAHOO SUMMARY BRIDGE] unsupported interval=%s skip", interval)
            continue

        df_interval = resample_ohlcv(df_1min, interval=interval)

        if df_interval.empty:
            logger.warning("[YAHOO SUMMARY BRIDGE] resampled empty interval=%s", interval)
            continue

        df_interval = enrich_yahoo_summary_df(
            df_interval,
            interval=interval,
            run_indicators=run_indicators,
            run_atr_slope=run_atr_slope,
            run_scoring=run_scoring,
        )

        if save:
            saved = save_yahoo_summary_df(
                df_interval,
                summary_db_path=summary_db_path,
                interval=interval,
            )
        else:
            saved = 0

        logger.info(
            "[YAHOO SUMMARY BRIDGE] done interval=%s rows=%s saved=%s symbols=%s",
            interval,
            len(df_interval),
            saved,
            df_interval["symbol"].nunique() if "symbol" in df_interval.columns else 0,
        )

        result[interval] = df_interval

    return result


# ============================================================
# Compatibility wrapper
# ============================================================

def run_yahoo_summary_bridge(
    *,
    yahoo_db_path: str | Path,
    summary_db_path: str | Path,
    interval: int | None = None,
    intervals: Iterable[int] | None = None,
    symbols: list[str] | None = None,
    start_dt: dt.datetime | str | None = None,
    end_dt: dt.datetime | str | None = None,
    save: bool = True,
) -> dict[int, pd.DataFrame]:
    if intervals is None:
        intervals = [interval] if interval else SUMMARY_INTERVALS

    return build_yahoo_summary_from_db(
        yahoo_db_path=yahoo_db_path,
        summary_db_path=summary_db_path,
        symbols=symbols,
        start_dt=start_dt,
        end_dt=end_dt,
        intervals=intervals,
        save=save,
    )


__all__ = [
    "load_yahoo_1min_df",
    "normalize_yahoo_1min_df",
    "resample_ohlcv",
    "enrich_yahoo_summary_df",
    "save_yahoo_summary_df",
    "build_yahoo_summary_from_db",
    "run_yahoo_summary_bridge",
]