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
from nas_root import nas_path, nas_root


DEFAULT_BASE_DIR = nas_root()
DEFAULT_KABU_STATION_DIR = os.path.join(
    DEFAULT_BASE_DIR,
    "raw_data",
    "kabu_station",
)
def _default_ranking_dir() -> str:
    """ranking DB の置き場所。

    2026-09-07: 既定を NAS からローカルへ移した。NAS 上の ranking DB は
    427MB + WAL 129MB まで育ち、`ranking_snapshot_1min` の読み取りが
    2 秒の予算に間に合わず err=interrupted になっていた。その結果
    候補が作れず (`no_ranking_df`)、**毎日エントリーが 1 件も出ていなかった**。
    NAS へは trading/ranking/ranking_db_location.py が定期複製する。
    """
    try:
        from trading.ranking import ranking_db_location

        return str(ranking_db_location.ranking_db_dir())
    except Exception:
        return os.path.join(DEFAULT_KABU_STATION_DIR, "ranking")


# 後方互換: モジュール定数として参照している箇所があるので残す。
# ただし import 時に固定されるので、既定値の解決は _default_ranking_dir() を使う。
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
    ranking_dir: str | None = None,
) -> str:
    """ranking DB のフルパス。

    ranking_dir を省略すると現在の既定 (通常はローカル) を使う。
    既定値を引数のデフォルトに固定すると import 時に決まってしまい、
    env での切り替えが効かなくなるので None 経由にしている。
    """
    ymd = normalize_yyyymmdd(trade_date)
    base = ranking_dir if ranking_dir else _default_ranking_dir()
    return str(Path(base) / f"ranking{ymd}.db")


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