# ============================================================
# File   : database/paths/ranking_paths.py
# Version: PRODUCTION-STABLE-REV1.0-DATABASE-RANKING-PATHS
# ------------------------------------------------------------
# 【概要】
#   ranking DB / ranking summary DB 用パス解決。
# ============================================================

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"
DEFAULT_KABU_STATION_DIR = os.path.join(
    DEFAULT_BASE_DIR,
    "raw_data",
    "kabu_station",
)
DEFAULT_RANKING_DIR = os.path.join(
    DEFAULT_KABU_STATION_DIR,
    "ranking",
)


def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def normalize_yyyymmdd(value: Any = None) -> str:
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

    try:
        return pd.to_datetime(s).strftime("%Y%m%d")
    except Exception:
        return _today_yyyymmdd()


def get_ranking_db_path(
    trade_date: Any = None,
    *,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> str:
    ymd = normalize_yyyymmdd(trade_date)
    return str(Path(ranking_dir) / f"ranking{ymd}.db")


def resolve_ranking_db_path(
    *,
    base_dir: str | None = None,
    ranking_dir: str | None = None,
    ymd: str | None = None,
    trade_date: Any = None,
) -> str:
    if ranking_dir:
        return get_ranking_db_path(trade_date or ymd, ranking_dir=ranking_dir)

    if base_dir:
        return str(Path(base_dir) / "ranking" / f"ranking{normalize_yyyymmdd(trade_date or ymd)}.db")

    return get_ranking_db_path(trade_date or ymd)


__all__ = [
    "DEFAULT_BASE_DIR",
    "DEFAULT_KABU_STATION_DIR",
    "DEFAULT_RANKING_DIR",
    "normalize_yyyymmdd",
    "get_ranking_db_path",
    "resolve_ranking_db_path",
]