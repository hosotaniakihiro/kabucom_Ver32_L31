# ============================================================
# File   : core/startup/summary_runtime_pkg/db_seed_policy.py
# Version: REV1.0-SUMMARY-RUNTIME-DB-SEED-POLICY
# ------------------------------------------------------------
# 【概要】
#   起動時 summary DB seed 用の設定値・共通 policy
#
# 【主な機能】
#   ✔ interval ごとの履歴本数
#   ✔ interval ごとの不足判定閾値
#   ✔ interval ごとの summary table 名
#   ✔ seed 本数取得 helper
# ============================================================

from __future__ import annotations

from typing import Any

import pandas as pd

from .state import SUMMARY_DB_SEED_BARS_PER_SYMBOL


BOOT_HISTORY_BARS_BY_TF = {
    1: 180,
    3: 120,
    5: 90,
}

BOOT_HISTORY_REQUIRED_HINT_BY_TF = {
    1: 75,
    3: 50,
    5: 40,
}

SUMMARY_TABLE_BY_TF = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}


def get_seed_bars(tf: int) -> int:
    """
    起動時に使う履歴本数。

    優先:
      1. BOOT_HISTORY_BARS_BY_TF
      2. state.SUMMARY_DB_SEED_BARS_PER_SYMBOL
      3. fallback 150
    """
    try:
        tf = int(tf)
    except Exception:
        tf = 1

    forced = BOOT_HISTORY_BARS_BY_TF.get(tf)
    if forced:
        return int(forced)

    try:
        return int(SUMMARY_DB_SEED_BARS_PER_SYMBOL.get(tf, 150))
    except Exception:
        return 150


def get_required_hint(tf: int, bars: int) -> int:
    """
    indicator 計算に最低限ほしい履歴本数の目安。
    """
    try:
        tf = int(tf)
    except Exception:
        tf = 1

    hint = BOOT_HISTORY_REQUIRED_HINT_BY_TF.get(tf, 50)
    return min(int(bars), int(hint))


def get_summary_table(tf: int) -> str | None:
    try:
        return SUMMARY_TABLE_BY_TF.get(int(tf))
    except Exception:
        return None


def as_df(df: Any) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df
    return pd.DataFrame()


def latest_dt(df: pd.DataFrame):
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "datetime" in df.columns:
            s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
            if not s.empty:
                return s.max()
    except Exception:
        pass
    return None


def nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return int((s != 0).sum())
    except Exception:
        return 0


def nonnull_count(df: pd.DataFrame, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return 0
        return int(df[col].notna().sum())
    except Exception:
        return 0


__all__ = [
    "BOOT_HISTORY_BARS_BY_TF",
    "BOOT_HISTORY_REQUIRED_HINT_BY_TF",
    "SUMMARY_TABLE_BY_TF",
    "get_seed_bars",
    "get_required_hint",
    "get_summary_table",
    "as_df",
    "latest_dt",
    "nonzero_count",
    "nonnull_count",
]