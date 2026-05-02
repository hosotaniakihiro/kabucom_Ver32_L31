# ============================================================
# File   : trading/summary/persistence/row_reducer.py
# Version: Ver1.0-PRODUCTION-ROW-REDUCER
# ------------------------------------------------------------
# 機能:
# - symbol/datetime 重複圧縮
# - latest per symbol 圧縮
# - latest N per symbol 圧縮
# - recent minutes 抽出
# - interval=1 heavy save 時の自動行圧縮
# ------------------------------------------------------------
# 主な責務:
# - 1分足 bulk upsert の件数削減
# - SQLite lock / 重い保存の負荷軽減
# ============================================================

from __future__ import annotations

import logging
import os

import pandas as pd

from trading.summary.persistence.identity_builder import (
    ensure_required_identity_columns,
    normalize_datetime_columns,
)

logger = logging.getLogger(__name__)

ENABLE_INTERVAL1_ROW_REDUCTION = True
INTERVAL1_REDUCTION_TRIGGER_ROWS = 2500
ENABLE_INTERVAL1_LATEST_PER_SYMBOL = False

INTERVAL1_RECENT_MINUTES = 3
INTERVAL1_LATEST_N_FIRST = 2
INTERVAL1_LATEST_N_FINAL = 1

ENV_ENABLE_INTERVAL1_LATEST_PER_SYMBOL = os.getenv(
    "SUMMARY_ENABLE_INTERVAL1_LATEST_PER_SYMBOL",
    ""
).strip().lower() in {"1", "true", "yes", "on"}


def dedupe_symbol_datetime_latest(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_datetime_columns(df)
    if df.empty:
        return df

    if "symbol" not in df.columns or "datetime" not in df.columns:
        return df

    try:
        return (
            df.dropna(subset=["symbol", "datetime"])
            .sort_values(["symbol", "datetime"], kind="mergesort")
            .drop_duplicates(["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )
    except Exception:
        logger.exception("[SUMMARY] dedupe_symbol_datetime_latest failed")
        return df


def reduce_to_latest_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_datetime_columns(df)
    if df.empty:
        return df

    if "symbol" not in df.columns or "datetime" not in df.columns:
        return df

    try:
        return (
            df.dropna(subset=["symbol", "datetime"])
            .sort_values(["symbol", "datetime"], kind="mergesort")
            .drop_duplicates(["symbol"], keep="last")
            .reset_index(drop=True)
        )
    except Exception:
        logger.exception("[SUMMARY] reduce_to_latest_per_symbol failed")
        return df


def reduce_to_latest_n_per_symbol(df: pd.DataFrame, n: int = 1) -> pd.DataFrame:
    df = normalize_datetime_columns(df)
    if df.empty:
        return df

    if "symbol" not in df.columns or "datetime" not in df.columns:
        return df

    try:
        n = max(1, int(n))
        out = (
            df.dropna(subset=["symbol", "datetime"])
            .sort_values(["symbol", "datetime"], kind="mergesort")
            .groupby("symbol", group_keys=False)
            .tail(n)
            .reset_index(drop=True)
        )
        return out
    except Exception:
        logger.exception("[SUMMARY] reduce_to_latest_n_per_symbol failed")
        return df


def keep_recent_minutes_only(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    df = normalize_datetime_columns(df)
    if df.empty or "datetime" not in df.columns:
        return df

    try:
        out = df.copy()
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        max_dt = out["datetime"].max()
        if pd.isna(max_dt):
            return out

        cutoff = max_dt - pd.Timedelta(minutes=max(1, int(minutes)))
        kept = out.loc[out["datetime"] >= cutoff].copy()

        logger.warning(
            "[SUMMARY] keep recent minutes only rows=%s -> %s max_dt=%s cutoff=%s",
            len(out),
            len(kept),
            max_dt,
            cutoff,
        )
        return kept.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY] keep_recent_minutes_only failed")
        return df


def maybe_reduce_upsert_rows(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    df = normalize_datetime_columns(df)
    if df.empty:
        return df

    interval = int(interval)
    before = len(df)
    reduced = df

    try:
        reduced = dedupe_symbol_datetime_latest(reduced)
        reduced = ensure_required_identity_columns(reduced, interval)
        after_key_dedupe = len(reduced)

        if after_key_dedupe != before:
            logger.warning(
                "[SUMMARY] row reduction dedupe(symbol,datetime) interval=%s rows=%s -> %s",
                interval,
                before,
                after_key_dedupe,
            )

        enable_latest_per_symbol = (
            ENABLE_INTERVAL1_LATEST_PER_SYMBOL
            or ENV_ENABLE_INTERVAL1_LATEST_PER_SYMBOL
        )

        if (
            ENABLE_INTERVAL1_ROW_REDUCTION
            and interval == 1
            and len(reduced) >= INTERVAL1_REDUCTION_TRIGGER_ROWS
        ):
            logger.warning(
                "[SUMMARY] interval=1 heavy save detected rows=%s trigger=%s latest_per_symbol=%s",
                len(reduced),
                INTERVAL1_REDUCTION_TRIGGER_ROWS,
                enable_latest_per_symbol,
            )

            tmp_recent = keep_recent_minutes_only(reduced, minutes=INTERVAL1_RECENT_MINUTES)
            if tmp_recent is not None and not tmp_recent.empty and len(tmp_recent) < len(reduced):
                logger.warning(
                    "[SUMMARY] row reduction recent-%smin interval=%s rows=%s -> %s",
                    INTERVAL1_RECENT_MINUTES,
                    interval,
                    len(reduced),
                    len(tmp_recent),
                )
                reduced = ensure_required_identity_columns(tmp_recent, interval)

            if enable_latest_per_symbol:
                tmp = reduce_to_latest_per_symbol(reduced)
                if tmp is not None and not tmp.empty and len(tmp) <= len(reduced):
                    logger.warning(
                        "[SUMMARY] row reduction latest-per-symbol interval=%s rows=%s -> %s",
                        interval,
                        len(reduced),
                        len(tmp),
                    )
                    reduced = ensure_required_identity_columns(tmp, interval)

            if len(reduced) >= INTERVAL1_REDUCTION_TRIGGER_ROWS:
                tmp2 = reduce_to_latest_n_per_symbol(reduced, n=INTERVAL1_LATEST_N_FIRST)
                if tmp2 is not None and not tmp2.empty and len(tmp2) < len(reduced):
                    logger.warning(
                        "[SUMMARY] row reduction latest-%s-per-symbol interval=%s rows=%s -> %s",
                        INTERVAL1_LATEST_N_FIRST,
                        interval,
                        len(reduced),
                        len(tmp2),
                    )
                    reduced = ensure_required_identity_columns(tmp2, interval)

            if len(reduced) >= INTERVAL1_REDUCTION_TRIGGER_ROWS:
                tmp1 = reduce_to_latest_n_per_symbol(reduced, n=INTERVAL1_LATEST_N_FINAL)
                if tmp1 is not None and not tmp1.empty and len(tmp1) < len(reduced):
                    logger.warning(
                        "[SUMMARY] row reduction latest-%s-per-symbol interval=%s rows=%s -> %s",
                        INTERVAL1_LATEST_N_FINAL,
                        interval,
                        len(reduced),
                        len(tmp1),
                    )
                    reduced = ensure_required_identity_columns(tmp1, interval)

        return reduced

    except Exception:
        logger.exception("[SUMMARY] maybe_reduce_upsert_rows failed interval=%s", interval)
        return df