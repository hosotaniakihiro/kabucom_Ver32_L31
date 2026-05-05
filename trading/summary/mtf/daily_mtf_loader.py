# ============================================================
# File   : trading/summary/mtf/daily_mtf_loader.py
# Version: PRODUCTION-STABLE-DAILY-MTF-LOADER-REV1.0
# ------------------------------------------------------------
# Purpose:
#   - stock_analysis.db の stock_analysis_latest から
#     日足 MA-MTF 用データを読み込む
#
# Source DB:
#   \\192.168.0.22\kabu\stock_data\daily_db\stock_analysis.db
#
# Source table:
#   stock_analysis_latest
#
# Output columns:
#   symbol
#   symbolname
#   market
#   daily_date
#   daily_close
#   daily_ma5
#   daily_ma25
#   daily_ma75
#   daily_ma5_slope
#   daily_ma25_slope
#   daily_ma75_slope
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


DEFAULT_DAILY_MTF_DB_PATH = (
    r"\\192.168.0.22\kabu\stock_data\daily_db\stock_analysis.db"
)

DEFAULT_DAILY_MTF_TABLE = "stock_analysis_latest"


_REQUIRED_COLUMNS = [
    "stock_code",
    "stock_name",
    "market",
    "date",
    "close",
    "MA_5",
    "MA_25",
    "MA_75",
    "MA_5_Slope",
    "MA_25_Slope",
    "MA_75_Slope",
]


def _normalize_symbol_series(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"\.T$", "", regex=True)
    )


def _safe_num(s: pd.Series, default: float = 0.0) -> pd.Series:
    return (
        pd.to_numeric(s, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(default)
    )


def _read_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    return {str(row[1]) for row in cur.fetchall()}


def load_daily_mtf_latest_df(
    *,
    db_path: str = DEFAULT_DAILY_MTF_DB_PATH,
    table_name: str = DEFAULT_DAILY_MTF_TABLE,
    min_close: float = 1.0,
) -> pd.DataFrame:
    """
    日足MA-MTF用の latest データを読み込む。

    Returns
    -------
    pd.DataFrame
        columns:
          symbol
          symbolname
          market
          daily_date
          daily_close
          daily_ma5
          daily_ma25
          daily_ma75
          daily_ma5_slope
          daily_ma25_slope
          daily_ma75_slope
    """
    if not db_path:
        logger.warning("[DAILY MTF LOADER] db_path empty")
        return pd.DataFrame()

    if not os.path.exists(db_path):
        logger.warning("[DAILY MTF LOADER] db not found: %s", db_path)
        return pd.DataFrame()

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            existing_cols = _read_table_columns(conn, table_name)
            missing = [c for c in _REQUIRED_COLUMNS if c not in existing_cols]
            if missing:
                logger.warning(
                    "[DAILY MTF LOADER] required columns missing table=%s missing=%s existing=%s",
                    table_name,
                    missing,
                    sorted(existing_cols),
                )
                return pd.DataFrame()

            sql = f"""
                SELECT
                    stock_code,
                    stock_name,
                    market,
                    date,
                    close,
                    MA_5,
                    MA_25,
                    MA_75,
                    MA_5_Slope,
                    MA_25_Slope,
                    MA_75_Slope
                FROM {table_name}
            """
            df = pd.read_sql_query(sql, conn)
    except Exception:
        logger.exception(
            "[DAILY MTF LOADER] failed to read db=%s table=%s",
            db_path,
            table_name,
        )
        return pd.DataFrame()

    if df.empty:
        logger.warning("[DAILY MTF LOADER] empty db=%s table=%s", db_path, table_name)
        return pd.DataFrame()

    out = pd.DataFrame()
    out["symbol"] = _normalize_symbol_series(df["stock_code"])
    out["symbolname"] = df["stock_name"].astype(str).fillna("")
    out["market"] = df["market"].astype(str).fillna("")
    out["daily_date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    out["daily_close"] = _safe_num(df["close"])
    out["daily_ma5"] = _safe_num(df["MA_5"])
    out["daily_ma25"] = _safe_num(df["MA_25"])
    out["daily_ma75"] = _safe_num(df["MA_75"])

    out["daily_ma5_slope"] = _safe_num(df["MA_5_Slope"])
    out["daily_ma25_slope"] = _safe_num(df["MA_25_Slope"])
    out["daily_ma75_slope"] = _safe_num(df["MA_75_Slope"])

    out = out[out["symbol"].ne("")]
    out = out[out["daily_close"] >= float(min_close)]

    out = (
        out.sort_values(["symbol", "daily_date"])
        .drop_duplicates(subset=["symbol"], keep="last")
        .reset_index(drop=True)
    )

    logger.info(
        "[DAILY MTF LOADER] loaded rows=%s symbols=%s latest_date=%s db=%s table=%s",
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["daily_date"].max() if "daily_date" in out.columns and not out.empty else None,
        db_path,
        table_name,
    )

    return out


def load_daily_mtf_latest_map(
    *,
    db_path: str = DEFAULT_DAILY_MTF_DB_PATH,
    table_name: str = DEFAULT_DAILY_MTF_TABLE,
) -> Dict[str, dict]:
    """
    symbol -> daily data dict の形で返す補助関数。
    """
    df = load_daily_mtf_latest_df(db_path=db_path, table_name=table_name)
    if df.empty:
        return {}
    return df.set_index("symbol").to_dict(orient="index")
