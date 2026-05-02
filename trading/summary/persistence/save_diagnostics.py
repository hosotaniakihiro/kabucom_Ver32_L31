# ============================================================
# File   : trading/summary/persistence/save_diagnostics.py
# Version: Ver1.0-PRODUCTION-SAVE-DIAGNOSTICS
# ------------------------------------------------------------
# 機能:
# - 保存前後の診断ログ
# - identity列の有無チェック
# - upsert密度ログ
# - DataFrame shape ログ
# - source分布/重複密度の可視化
# ------------------------------------------------------------
# 主な責務:
# - summary保存時の観測性向上
# - 不具合切り分け用の診断ログ出力
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.summary.persistence.dataframe_utils import safe_get_series

logger = logging.getLogger(__name__)


def log_identity_columns(prefix: str, df: pd.DataFrame, interval: int) -> None:
    try:
        if df is None or df.empty:
            logger.warning("%s interval=%s empty df", prefix, interval)
            return

        cols = list(df.columns)
        symbol_s = safe_get_series(df, "symbol")
        datetime_s = safe_get_series(df, "datetime")
        date_s = safe_get_series(df, "date")
        time_range_s = safe_get_series(df, "time_range")

        has_symbol = symbol_s is not None and symbol_s.notna().any()
        has_datetime = datetime_s is not None and datetime_s.notna().any()
        has_date = date_s is not None and date_s.notna().any()
        has_time_range = time_range_s is not None and time_range_s.notna().any()

        logger.info(
            "%s interval=%s rows=%s has_symbol=%s has_datetime=%s has_date=%s has_time_range=%s cols=%s",
            prefix,
            interval,
            len(df),
            has_symbol,
            has_datetime,
            has_date,
            has_time_range,
            cols,
        )
    except Exception:
        logger.exception("[SUMMARY] identity log failed interval=%s", interval)


def log_upsert_density(prefix: str, df: pd.DataFrame, interval: int) -> None:
    try:
        if df is None or df.empty:
            logger.warning("%s interval=%s empty", prefix, interval)
            return

        rows = len(df)
        symbol_count = 0
        datetime_count = 0
        min_dt = None
        max_dt = None
        avg_rows_per_symbol = 0.0
        max_rows_per_symbol = 0
        dup_key_rows = 0

        if "symbol" in df.columns:
            symbol_s = safe_get_series(df, "symbol")
            if symbol_s is not None:
                symbol_count = int(symbol_s.dropna().astype(str).str.strip().nunique())

                try:
                    vc = (
                        symbol_s.dropna()
                        .astype(str)
                        .str.strip()
                        .value_counts(dropna=False)
                    )
                    if len(vc) > 0:
                        avg_rows_per_symbol = float(rows / max(1, symbol_count))
                        max_rows_per_symbol = int(vc.max())
                except Exception:
                    logger.debug("[SUMMARY] rows/symbol stats failed", exc_info=True)

        if "datetime" in df.columns:
            dt_s = pd.to_datetime(safe_get_series(df, "datetime"), errors="coerce")
            if dt_s is not None:
                dt_valid = dt_s.dropna()
                datetime_count = int(dt_valid.nunique()) if len(dt_valid) > 0 else 0
                min_dt = dt_valid.min() if len(dt_valid) > 0 else None
                max_dt = dt_valid.max() if len(dt_valid) > 0 else None

        try:
            if "symbol" in df.columns and "datetime" in df.columns:
                dup_key_rows = int(df.duplicated(subset=["symbol", "datetime"], keep=False).sum())
        except Exception:
            logger.debug("[SUMMARY] duplicate density calc failed", exc_info=True)

        logger.warning(
            "%s interval=%s rows=%s symbols=%s datetimes=%s avg_rows_per_symbol=%.2f max_rows_per_symbol=%s dup_key_rows=%s dt_range=%s -> %s",
            prefix,
            interval,
            rows,
            symbol_count,
            datetime_count,
            avg_rows_per_symbol,
            max_rows_per_symbol,
            dup_key_rows,
            min_dt,
            max_dt,
        )

        try:
            if "source" in df.columns:
                src_counts = (
                    df["source"]
                    .fillna("NULL")
                    .astype(str)
                    .value_counts(dropna=False)
                    .to_dict()
                )
                logger.warning("%s source_dist interval=%s %s", prefix, interval, src_counts)
        except Exception:
            logger.debug("[SUMMARY] source density log failed", exc_info=True)

        try:
            if "symbol" in df.columns and "datetime" in df.columns:
                base_cols = ["symbol", "datetime"]
                if "source" in df.columns:
                    base_cols.append("source")
                sample = df[base_cols].copy()
                sample["datetime"] = pd.to_datetime(sample["datetime"], errors="coerce")
                sample = sample.sort_values(["symbol", "datetime"], kind="stable")
                logger.info(
                    "%s sample interval=%s\n%s",
                    prefix,
                    interval,
                    sample.head(20).to_string(index=False),
                )
        except Exception:
            logger.debug("[SUMMARY] density sample log failed", exc_info=True)

    except Exception:
        logger.exception("[SUMMARY] log_upsert_density failed interval=%s", interval)


def log_df_shape(prefix: str, df: pd.DataFrame, interval: int) -> None:
    try:
        rows = 0 if df is None else len(df)
        cols = 0 if df is None else len(df.columns)
        logger.debug("%s rows=%d interval=%s cols=%d", prefix, rows, interval, cols)
    except Exception:
        logger.exception("[SUMMARY] shape log failed: interval=%s", interval)