# ============================================================
# File   : trading/summary/recovery/tail_processors.py
# Ver    : PRODUCTION-STABLE-REV1.0-TAIL-PROCESSORS
# ------------------------------------------------------------
# 【概要】
#   indicator / scoring を symbolごとの末尾だけ再計算する軽量 wrapper
#
# 【目的】
#   - 起動時 bootstrap の高速化
#   - 全履歴への重い再計算を避ける
#   - 必要な warmup を確保しつつ末尾だけ再評価する
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.summary.recovery.helpers import normalize_datetime_columns
from .bootstrap_transforms import apply_indicators_and_scoring

logger = logging.getLogger(__name__)


def _ensure_sorted_symbol_dt(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = normalize_datetime_columns(df, interval=interval)

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str)

    if {"symbol", "datetime"}.issubset(out.columns):
        out = out.sort_values(["symbol", "datetime"], kind="stable")

    return out.reset_index(drop=True)


def _tail_by_symbol(df: pd.DataFrame, bars: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if not {"symbol", "datetime"}.issubset(df.columns):
        return df.copy().reset_index(drop=True)

    return (
        df.sort_values(["symbol", "datetime"], kind="stable")
        .groupby("symbol", group_keys=False)
        .tail(max(int(bars), 1))
        .reset_index(drop=True)
    )


def _head_excluding_tail(df: pd.DataFrame, bars: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if not {"symbol", "datetime"}.issubset(df.columns):
        return pd.DataFrame()

    x = df.sort_values(["symbol", "datetime"], kind="stable").copy()
    tail_index = (
        x.groupby("symbol", group_keys=False)
        .tail(max(int(bars), 1))
        .index
    )

    return x.drop(index=tail_index, errors="ignore").reset_index(drop=True)


def _merge_preserving_latest(head_df: pd.DataFrame, tail_df: pd.DataFrame, interval: int) -> pd.DataFrame:
    parts = []

    if isinstance(head_df, pd.DataFrame) and not head_df.empty:
        parts.append(head_df)

    if isinstance(tail_df, pd.DataFrame) and not tail_df.empty:
        parts.append(tail_df)

    if not parts:
        return pd.DataFrame()

    out = pd.concat(parts, axis=0, ignore_index=True)
    out = normalize_datetime_columns(out, interval=interval)

    if {"symbol", "datetime"}.issubset(out.columns):
        out["symbol"] = out["symbol"].astype(str)
        out = (
            out.sort_values(["symbol", "datetime"], kind="stable")
            .drop_duplicates(["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )

    return out


def apply_indicators_and_scoring_tail(
    df: pd.DataFrame,
    *,
    interval: int,
    label: str,
    tail_bars: int,
    safety_margin: int = 20,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    full = _ensure_sorted_symbol_dt(df, interval=interval)
    if full.empty:
        return pd.DataFrame()

    calc_bars = max(int(tail_bars), 1) + max(int(safety_margin), 0)

    head_df = _head_excluding_tail(full, calc_bars)
    tail_df = _tail_by_symbol(full, calc_bars)

    logger.info(
        "[summary_recovery.tail] start interval=%s label=%s full_rows=%d head_rows=%d tail_rows=%d calc_bars=%d",
        interval,
        label,
        len(full),
        len(head_df),
        len(tail_df),
        calc_bars,
    )

    if tail_df.empty:
        return full

    tail_calc = apply_indicators_and_scoring(
        tail_df,
        interval=interval,
        label=label,
    )
    tail_calc = _ensure_sorted_symbol_dt(tail_calc, interval=interval)

    out = _merge_preserving_latest(head_df, tail_calc, interval=interval)

    logger.info(
        "[summary_recovery.tail] done interval=%s label=%s out_rows=%d",
        interval,
        label,
        len(out),
    )
    return out


__all__ = [
    "apply_indicators_and_scoring_tail",
]