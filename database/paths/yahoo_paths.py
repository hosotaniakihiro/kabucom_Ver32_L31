# ============================================================
# File   : database/paths/yahoo_paths.py
# Version: PRODUCTION-STABLE-REV1.0-YAHOO-PATHS
# ------------------------------------------------------------
# Purpose:
#   Yahoo 1min intraday DB path resolver.
#
# Notes:
#   - Existing config.paths.get_path("raw_yahoo_intraday") is used
#     when available.
#   - Falls back to NAS default path when config is unavailable.
# ============================================================

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

try:
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover
    pd = None  # type: ignore


DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"
DEFAULT_YAHOO_INTRADAY_DIR = os.path.join(
    DEFAULT_BASE_DIR,
    "raw_data",
    "yahoo",
    "intraday",
)


def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def normalize_yyyymmdd(value: Any = None) -> str:
    """Normalize date-like value to YYYYMMDD."""
    if value is None:
        return _today_yyyymmdd()

    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")

    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")

    s = str(value).strip()
    if not s:
        return _today_yyyymmdd()

    if len(s) == 8 and s.isdigit():
        return s

    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "")

    try:
        if pd is not None:
            return pd.to_datetime(s).strftime("%Y%m%d")
    except Exception:
        pass

    try:
        return dt.datetime.fromisoformat(s).strftime("%Y%m%d")
    except Exception:
        return _today_yyyymmdd()


def normalize_trade_date(value: Any = None) -> str:
    """Normalize date-like value to YYYY-MM-DD."""
    ymd = normalize_yyyymmdd(value)
    return f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"


def get_yahoo_intraday_dir(*, yahoo_dir: str | None = None) -> Path:
    if yahoo_dir:
        return Path(yahoo_dir)

    try:
        from config.paths import get_path  # type: ignore

        p = get_path("raw_yahoo_intraday")
        if p:
            return Path(p)
    except Exception:
        pass

    return Path(DEFAULT_YAHOO_INTRADAY_DIR)


def get_yahoo_1min_db_path(
    trade_date: Any = None,
    *,
    yahoo_dir: str | None = None,
) -> str:
    ymd = normalize_yyyymmdd(trade_date)
    base = get_yahoo_intraday_dir(yahoo_dir=yahoo_dir)
    return str(base / f"yahoo_1min_{ymd}.db")


def resolve_yahoo_1min_db_path(
    *,
    base_dir: str | None = None,
    yahoo_dir: str | None = None,
    ymd: str | None = None,
    trade_date: Any = None,
) -> str:
    if yahoo_dir:
        return get_yahoo_1min_db_path(trade_date or ymd, yahoo_dir=yahoo_dir)

    if base_dir:
        y = normalize_yyyymmdd(trade_date or ymd)
        return str(Path(base_dir) / "raw_data" / "yahoo" / "intraday" / f"yahoo_1min_{y}.db")

    return get_yahoo_1min_db_path(trade_date or ymd)


__all__ = [
    "DEFAULT_BASE_DIR",
    "DEFAULT_YAHOO_INTRADAY_DIR",
    "normalize_yyyymmdd",
    "normalize_trade_date",
    "get_yahoo_intraday_dir",
    "get_yahoo_1min_db_path",
    "resolve_yahoo_1min_db_path",
]
