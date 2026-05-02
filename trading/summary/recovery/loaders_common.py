# ============================================================
# File   : trading/summary/recovery/loaders_common.py
# Ver    : PRODUCTION-STABLE-REV1.1-LOADERS-COMMON-DUPCOL-FIX
# ------------------------------------------------------------
# ✔ loader 共通 helper
# ✔ datetime guard
# ✔ target_dates / symbols 正規化
# ✔ dataframe 日別 breakdown log
# ✔ generic dataframe filters
# ✔ duplicate columns safe
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

FUTURE_TOLERANCE_MINUTES = 2


def now_naive() -> pd.Timestamp:
    try:
        return pd.Timestamp.now().tz_localize(None)
    except Exception:
        return pd.Timestamp.now()


def _drop_duplicate_columns(
    df: pd.DataFrame,
    *,
    label: str,
) -> pd.DataFrame:
    try:
        if df is None or getattr(df, "empty", True):
            return df

        if not hasattr(df, "columns"):
            return df

        dup_mask = df.columns.duplicated()
        if not dup_mask.any():
            return df

        dup_cols = df.columns[dup_mask].tolist()
        logger.warning(
            "[summary.recovery.loaders_common] duplicate columns dropped label=%s dup_cols=%s",
            label,
            dup_cols,
        )
        return df.loc[:, ~df.columns.duplicated()].copy()

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_common] _drop_duplicate_columns failed label=%s",
            label,
        )
        return df


def _get_safe_series(
    df: pd.DataFrame,
    column: str,
    *,
    label: str,
) -> Optional[pd.Series]:
    try:
        if df is None or getattr(df, "empty", True):
            return None

        if not hasattr(df, "columns"):
            return None

        if column not in df.columns:
            return None

        obj = df[column]

        # 同名列重複時は DataFrame が返ることがある
        if isinstance(obj, pd.DataFrame):
            logger.warning(
                "[summary.recovery.loaders_common] duplicated target column resolved as DataFrame label=%s column=%s shape=%s -> first column used",
                label,
                column,
                obj.shape,
            )
            if obj.shape[1] <= 0:
                return None
            return obj.iloc[:, 0]

        if isinstance(obj, pd.Series):
            return obj

        try:
            return pd.Series(obj)
        except Exception:
            return None

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_common] _get_safe_series failed label=%s column=%s",
            label,
            column,
        )
        return None


def sanitize_checkpoint_dt(
    value: Optional[pd.Timestamp],
    *,
    label: str,
    interval: Optional[int] = None,
) -> Optional[pd.Timestamp]:
    try:
        if value is None or pd.isna(value):
            return None

        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None

        if getattr(ts, "tzinfo", None) is not None:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                try:
                    ts = ts.tz_convert(None)
                except Exception:
                    pass

        now_ts = now_naive()
        future_limit = now_ts + pd.Timedelta(minutes=FUTURE_TOLERANCE_MINUTES)

        if ts > future_limit:
            logger.warning(
                "[summary.recovery.loaders_common] future checkpoint ignored label=%s interval=%s value=%s now=%s tolerance_min=%s",
                label,
                interval,
                ts,
                now_ts,
                FUTURE_TOLERANCE_MINUTES,
            )
            return None

        return ts

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_common] sanitize checkpoint failed label=%s interval=%s value=%s",
            label,
            interval,
            value,
        )
        return None


def sanitize_query_dt(
    value: Optional[pd.Timestamp],
    *,
    label: str,
) -> Optional[pd.Timestamp]:
    try:
        if value is None or pd.isna(value):
            return None

        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None

        if getattr(ts, "tzinfo", None) is not None:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                try:
                    ts = ts.tz_convert(None)
                except Exception:
                    pass

        return ts

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_common] sanitize query dt failed label=%s value=%s",
            label,
            value,
        )
        return None


def coerce_date_set(target_dates: Optional[Iterable]) -> set[str]:
    out: set[str] = set()
    try:
        if not target_dates:
            return out

        for x in target_dates:
            if x is None:
                continue
            try:
                ts = pd.to_datetime(x, errors="coerce")
                if pd.notna(ts):
                    out.add(ts.strftime("%Y-%m-%d"))
                    continue
            except Exception:
                pass

            s = str(x).strip()
            if not s:
                continue

            if len(s) >= 10 and "-" in s:
                out.add(s[:10])
            elif len(s) == 8 and s.isdigit():
                out.add(f"{s[:4]}-{s[4:6]}-{s[6:8]}")

        return out

    except Exception:
        logger.exception("[summary.recovery.loaders_common] coerce_date_set failed")
        return out


def normalize_symbols(symbols: Optional[Iterable]) -> list[str]:
    out: list[str] = []
    try:
        if not symbols:
            return out

        for s in symbols:
            x = str(s).strip()
            if not x or x.lower() in {"nan", "none", "nat"}:
                continue
            if "." in x:
                x = x.split(".", 1)[0].strip()
            if x not in out:
                out.append(x)

        return out

    except Exception:
        logger.exception("[summary.recovery.loaders_common] normalize_symbols failed")
        return out


def log_df_date_breakdown(
    df: pd.DataFrame,
    *,
    label: str,
    datetime_col: str = "datetime",
) -> None:
    try:
        if df is None or df.empty:
            logger.info("[summary.recovery.loaders_common] %s breakdown empty", label)
            return

        x = df.copy()
        x = _drop_duplicate_columns(x, label=f"{label}.breakdown")

        if datetime_col not in x.columns:
            logger.info(
                "[summary.recovery.loaders_common] %s breakdown skipped reason=no_%s rows=%s cols=%s",
                label,
                datetime_col,
                len(x),
                list(x.columns),
            )
            return

        dt_s = _get_safe_series(x, datetime_col, label=f"{label}.breakdown")
        if dt_s is None:
            logger.info(
                "[summary.recovery.loaders_common] %s breakdown skipped reason=datetime_series_unavailable",
                label,
            )
            return

        x[datetime_col] = pd.to_datetime(dt_s, errors="coerce")
        x = x[x[datetime_col].notna()].copy()

        if x.empty:
            logger.info(
                "[summary.recovery.loaders_common] %s breakdown skipped reason=no_valid_datetime",
                label,
            )
            return

        x["date_only"] = x[datetime_col].dt.strftime("%Y-%m-%d")

        if "symbol" in x.columns:
            grouped = (
                x.groupby("date_only")
                .agg(
                    rows=("date_only", "size"),
                    symbols=("symbol", "nunique"),
                    dt_min=(datetime_col, "min"),
                    dt_max=(datetime_col, "max"),
                )
                .reset_index()
                .sort_values("date_only")
            )
            total_symbols = int(x["symbol"].nunique())
        else:
            grouped = (
                x.groupby("date_only")
                .agg(
                    rows=("date_only", "size"),
                    dt_min=(datetime_col, "min"),
                    dt_max=(datetime_col, "max"),
                )
                .reset_index()
                .sort_values("date_only")
            )
            grouped["symbols"] = 0
            total_symbols = 0

        logger.info(
            "[summary.recovery.loaders_common] %s breakdown total_rows=%s total_symbols=%s",
            label,
            len(x),
            total_symbols,
        )

        for _, row in grouped.iterrows():
            logger.info(
                "[summary.recovery.loaders_common] %s by_date=%s rows=%s symbols=%s dt_min=%s dt_max=%s",
                label,
                row["date_only"],
                int(row["rows"]),
                int(row["symbols"]) if pd.notna(row["symbols"]) else 0,
                row["dt_min"],
                row["dt_max"],
            )

    except Exception:
        logger.exception("[summary.recovery.loaders_common] %s breakdown failed", label)


def apply_target_date_filter(
    df: pd.DataFrame,
    *,
    datetime_col: str = "datetime",
    target_dates: Optional[Iterable] = None,
    label: str = "",
) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return df

        allowed_dates = coerce_date_set(target_dates)
        if not allowed_dates:
            return df

        x = _drop_duplicate_columns(df.copy(), label=f"{label}.target_date_filter")

        if datetime_col not in x.columns:
            return x

        dt_s = _get_safe_series(x, datetime_col, label=f"{label}.target_date_filter")
        if dt_s is None:
            return x

        dt_s = pd.to_datetime(dt_s, errors="coerce")
        mask = dt_s.dt.strftime("%Y-%m-%d").isin(allowed_dates)

        out = x.loc[mask].copy()
        dropped = int(len(x) - len(out))
        if dropped > 0:
            logger.info(
                "[summary.recovery.loaders_common] target_dates filter applied label=%s before=%d after=%d dropped=%d allowed_dates=%s",
                label,
                len(x),
                len(out),
                dropped,
                sorted(allowed_dates),
            )
        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_common] apply_target_date_filter failed label=%s",
            label,
        )
        return df


def apply_max_allowed_dt_filter(
    df: pd.DataFrame,
    *,
    datetime_col: str = "datetime",
    max_allowed_dt: Optional[pd.Timestamp] = None,
    label: str = "",
) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return df

        if max_allowed_dt is None or pd.isna(max_allowed_dt):
            return df

        x = _drop_duplicate_columns(df.copy(), label=f"{label}.max_allowed_dt_filter")

        if datetime_col not in x.columns:
            return x

        max_allowed_dt = sanitize_query_dt(
            max_allowed_dt,
            label=f"{label}.max_allowed_dt",
        )
        if max_allowed_dt is None or pd.isna(max_allowed_dt):
            return x

        dt_s = _get_safe_series(x, datetime_col, label=f"{label}.max_allowed_dt_filter")
        if dt_s is None:
            return x

        dt_s = pd.to_datetime(dt_s, errors="coerce")
        mask = dt_s <= max_allowed_dt
        out = x.loc[mask].copy()

        dropped = int(len(x) - len(out))
        if dropped > 0:
            logger.info(
                "[summary.recovery.loaders_common] max_allowed_dt filter applied label=%s before=%d after=%d dropped=%d max_allowed_dt=%s",
                label,
                len(x),
                len(out),
                dropped,
                max_allowed_dt,
            )
        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_common] apply_max_allowed_dt_filter failed label=%s",
            label,
        )
        return df


__all__ = [
    "FUTURE_TOLERANCE_MINUTES",
    "now_naive",
    "sanitize_checkpoint_dt",
    "sanitize_query_dt",
    "coerce_date_set",
    "normalize_symbols",
    "log_df_date_breakdown",
    "apply_target_date_filter",
    "apply_max_allowed_dt_filter",
]