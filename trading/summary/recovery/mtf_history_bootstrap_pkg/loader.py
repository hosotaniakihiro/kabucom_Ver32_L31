# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/loader.py
# Version: PRODUCTION-STABLE-REV1.0-LOADER
# ------------------------------------------------------------
# 【概要】
#   summary DB から 1分足履歴を読む
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

from .constants import DEFAULT_HISTORY_BARS_1M, DEFAULT_LOOKBACK_DAYS
from .datetime_guard import runtime_cutoff_now, drop_future_datetime_rows
from .dataframe_utils import normalize_summary_df

logger = logging.getLogger(__name__)


def get_summary_engine():
    try:
        from database.session import get_summary_engine as _get_summary_engine
        return _get_summary_engine()
    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] get_summary_engine failed", exc_info=True)

    try:
        from database.session import summary_engine
        return summary_engine
    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] summary_engine import failed", exc_info=True)

    return None


def table_exists(engine, table_name: str) -> bool:
    try:
        if engine is None:
            return False

        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            return row is not None

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] table_exists failed table=%s", table_name, exc_info=True)
        return False


def get_table_columns(engine, table_name: str) -> set[str]:
    try:
        if engine is None:
            return set()

        with engine.connect() as conn:
            rows = conn.exec_driver_sql(f'PRAGMA table_info("{table_name}")').fetchall()

        return {
            str(r[1]).strip()
            for r in rows
            if len(r) > 1 and r[1] is not None and str(r[1]).strip()
        }

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] get columns failed table=%s", table_name, exc_info=True)
        return set()


def load_1m_summary_history(
    *,
    engine=None,
    symbols: Optional[Iterable[str]] = None,
    max_rows_per_symbol: int = DEFAULT_HISTORY_BARS_1M,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    engine = engine or get_summary_engine()
    if engine is None:
        logger.warning("[MTF HISTORY BOOTSTRAP] summary engine not found")
        return pd.DataFrame()

    table = "stock_summary_1min"

    if not table_exists(engine, table):
        logger.warning("[MTF HISTORY BOOTSTRAP] table not found: %s", table)
        return pd.DataFrame()

    cols = get_table_columns(engine, table)
    if not cols:
        logger.warning("[MTF HISTORY BOOTSTRAP] table columns empty: %s", table)
        return pd.DataFrame()

    wanted = [
        "symbol",
        "symbolname",
        "datetime",
        "date",
        "time",
        "time_range",
        "start_time",
        "end_time",
        "source",
        "open",
        "high",
        "low",
        "close",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "vwap",
        "ma5",
        "ma25",
        "ma75",
        "rsi",
        "macd",
        "signal",
        "hist",
        "atr",
        "slope",
        "slope_atr_scaled",
        "score_slope",
        "mtf",
        "score_mtf",
        "mtf_score",
        "score",
        "score_total",
        "display_score",
        "final_score",
        "score_buy",
        "score_sell",
        "buy_score",
        "sell_score",
        "display_ready",
        "technical_ready",
        "symbol_hist_len",
    ]

    select_cols = [c for c in wanted if c in cols]

    if "symbol" not in select_cols or "datetime" not in select_cols:
        logger.warning("[MTF HISTORY BOOTSTRAP] required columns missing cols=%s", sorted(cols))
        return pd.DataFrame()

    since = pd.Timestamp.now().normalize() - pd.Timedelta(days=max(int(lookback_days), 1))
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    cutoff = runtime_cutoff_now()
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    today_str = pd.Timestamp.now().strftime("%Y-%m-%d")

    sql = f"""
        SELECT {", ".join([f'"{c}"' for c in select_cols])}
        FROM "{table}"
        WHERE datetime >= :since
          AND (
                date(datetime) < :today
                OR datetime <= :cutoff
              )
        ORDER BY symbol, datetime
    """

    try:
        df = pd.read_sql_query(
            sql,
            engine,
            params={
                "since": since_str,
                "today": today_str,
                "cutoff": cutoff_str,
            },
        )
    except Exception:
        logger.exception("[MTF HISTORY BOOTSTRAP] read 1m history failed")
        return pd.DataFrame()

    df = normalize_summary_df(df)
    df = drop_future_datetime_rows(df, interval=1, label="load_1m_summary_history")

    if df.empty:
        logger.warning("[MTF HISTORY BOOTSTRAP] 1m history empty after normalize/future_guard")
        return df

    if symbols:
        symset = {str(s).strip().replace(".0", "") for s in symbols}
        df = df[df["symbol"].isin(symset)].copy()

    try:
        df = (
            df.sort_values(["symbol", "datetime"], kind="stable")
            .groupby("symbol", group_keys=False)
            .tail(max(int(max_rows_per_symbol), 30))
            .reset_index(drop=True)
        )
    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] per-symbol tail failed", exc_info=True)

    df = drop_future_datetime_rows(df, interval=1, label="load_1m_summary_history_tail")

    try:
        hist = df.groupby("symbol")["datetime"].nunique()
        logger.info(
            "[MTF HISTORY BOOTSTRAP] loaded 1m history rows=%s symbols=%s hist_min=%s hist_median=%.1f hist_max=%s dt_min=%s dt_max=%s cutoff=%s",
            len(df),
            df["symbol"].nunique() if "symbol" in df.columns else 0,
            int(hist.min()) if not hist.empty else 0,
            float(hist.median()) if not hist.empty else 0.0,
            int(hist.max()) if not hist.empty else 0,
            df["datetime"].min() if "datetime" in df.columns and not df.empty else None,
            df["datetime"].max() if "datetime" in df.columns and not df.empty else None,
            cutoff,
        )
    except Exception:
        logger.info(
            "[MTF HISTORY BOOTSTRAP] loaded 1m history rows=%s symbols=%s",
            len(df),
            df["symbol"].nunique() if "symbol" in df.columns else 0,
        )

    return df


__all__ = [
    "get_summary_engine",
    "table_exists",
    "get_table_columns",
    "load_1m_summary_history",
]