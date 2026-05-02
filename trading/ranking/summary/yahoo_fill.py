# ============================================================
# File   : trading/ranking/summary/yahoo_fill.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-YAHOO-FILL
# ------------------------------------------------------------
# 【概要】
#   Yahoo 1分足 close によるランキング価格系列の補完
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from typing import Iterable, Optional

import pandas as pd

from trading.ranking.summary.constants import DATETIME_CANDIDATES
from trading.ranking.summary.utils import first_existing_col, normalize_symbols, path_exists

logger = logging.getLogger(__name__)


def try_load_yahoo_1min(
    *,
    yahoo_db_path: Optional[str],
    symbols: Optional[Iterable[str]],
    start_dt: Optional[pd.Timestamp],
    end_dt: Optional[pd.Timestamp],
) -> pd.DataFrame:
    if not yahoo_db_path:
        return pd.DataFrame()

    if not path_exists(yahoo_db_path):
        logger.debug(
            "[RANKING SUMMARY RUNNER] yahoo db not found path=%s",
            yahoo_db_path,
        )
        return pd.DataFrame()

    conn: Optional[sqlite3.Connection] = None

    try:
        conn = sqlite3.connect(yahoo_db_path, timeout=10.0)

        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'",
                conn,
            )["name"].astype(str).tolist()
        except Exception:
            return pd.DataFrame()

        candidate_tables = [
            t for t in tables
            if t.lower() in ("yahoo_1min", "intraday_1min", "price_1min", "ohlcv_1min")
            or "1min" in t.lower()
        ]

        if not candidate_tables:
            return pd.DataFrame()

        table = candidate_tables[0]

        sql = f"SELECT * FROM {table}"
        params: dict[str, object] = {}
        where = []

        symbol_list = normalize_symbols(symbols)

        if symbol_list:
            placeholders = []

            for i, sym in enumerate(symbol_list):
                key = f"ysym_{i}"
                placeholders.append(f":{key}")
                params[key] = sym

            where.append(f"CAST(symbol AS TEXT) IN ({','.join(placeholders)})")

        if where:
            sql += " WHERE " + " AND ".join(where)

        raw = pd.read_sql_query(sql, conn, params=params)

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] yahoo load failed path=%s",
            yahoo_db_path,
        )
        return pd.DataFrame()

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if raw.empty:
        return pd.DataFrame()

    try:
        y = raw.copy()

        dt_col = first_existing_col(y, DATETIME_CANDIDATES)
        price_col = first_existing_col(y, ("close", "current_price", "price"))

        if dt_col is None or price_col is None or "symbol" not in y.columns:
            return pd.DataFrame()

        y["datetime"] = pd.to_datetime(y[dt_col], errors="coerce")

        try:
            y["datetime"] = y["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        y["symbol"] = y["symbol"].astype(str).str.strip()
        y["close"] = pd.to_numeric(y[price_col], errors="coerce")

        y = y[
            y["datetime"].notna()
            & y["symbol"].ne("")
            & y["close"].notna()
            & (y["close"] > 0)
        ].copy()

        if start_dt is not None:
            y = y[y["datetime"] >= start_dt].copy()

        if end_dt is not None:
            y = y[y["datetime"] <= end_dt].copy()

        return y[["symbol", "datetime", "close"]].reset_index(drop=True)

    except Exception:
        logger.exception("[RANKING SUMMARY RUNNER] yahoo normalize failed")
        return pd.DataFrame()


def apply_yahoo_fill(
    base_df: pd.DataFrame,
    *,
    yahoo_db_path: Optional[str],
    symbols: Optional[Iterable[str]],
    use_yahoo_fill: bool,
) -> pd.DataFrame:
    if not use_yahoo_fill:
        return base_df

    if base_df is None or base_df.empty:
        return pd.DataFrame()

    if not yahoo_db_path:
        return base_df

    try:
        start_dt = pd.to_datetime(base_df["datetime"], errors="coerce").min()
        end_dt = pd.to_datetime(base_df["datetime"], errors="coerce").max()
    except Exception:
        start_dt = None
        end_dt = None

    yahoo = try_load_yahoo_1min(
        yahoo_db_path=yahoo_db_path,
        symbols=symbols,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    if yahoo.empty:
        return base_df

    x = base_df.copy()

    try:
        yahoo = yahoo.rename(columns={"close": "__yahoo_close__"})

        x = x.merge(
            yahoo,
            on=["symbol", "datetime"],
            how="left",
        )

        before_na = int(x["close"].isna().sum()) if "close" in x.columns else 0

        x["close"] = pd.to_numeric(x["close"], errors="coerce")
        x["__yahoo_close__"] = pd.to_numeric(
            x["__yahoo_close__"],
            errors="coerce",
        )

        x["close"] = x["close"].fillna(x["__yahoo_close__"])
        x["current_price"] = pd.to_numeric(
            x.get("current_price", x["close"]),
            errors="coerce",
        ).fillna(x["close"])

        x = x.drop(columns=["__yahoo_close__"], errors="ignore")

        after_na = int(x["close"].isna().sum())

        logger.info(
            "[RANKING SUMMARY RUNNER] yahoo fill applied before_na=%s after_na=%s",
            before_na,
            after_na,
        )

        return x

    except Exception:
        logger.exception("[RANKING SUMMARY RUNNER] yahoo fill failed")
        return base_df