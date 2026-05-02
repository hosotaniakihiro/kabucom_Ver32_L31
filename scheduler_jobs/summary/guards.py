# ============================================================
# File   : scheduler_jobs/summary/guards.py
# Ver    : PRODUCTION-STABLE-SUMMARY-GUARDS-V1
# ------------------------------------------------------------
# ✔ DataFrame防御処理を分離
# ✔ actual dates 抽出
# ✔ 営業日ベースの date guard
# ✔ explicit dates guard
# ✔ datetime missing 時の安全継続
# ✔ summary_jobs.py / recovery系から再利用しやすい構成
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

from .calendar_utils import get_closed_day_allowed_dates

logger = logging.getLogger(__name__)


# ============================================================
# basic helpers
# ============================================================

def ensure_dataframe(df) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        out = df.copy()
    elif isinstance(df, pd.Series):
        out = pd.DataFrame([df.to_dict()])
    elif isinstance(df, dict):
        out = pd.DataFrame([df])
    else:
        try:
            out = pd.DataFrame(df).copy()
        except Exception:
            logger.exception("[summary.guards] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass

    return out


def safe_get_series(df: pd.DataFrame, col: str) -> Optional[pd.Series]:
    try:
        if df is None or df.empty or col not in df.columns:
            return None

        value = df[col]

        if isinstance(value, pd.DataFrame):
            if value.shape[1] <= 0:
                return None

            out = None
            for i in range(value.shape[1]):
                s = value.iloc[:, i]
                if out is None:
                    out = s
                else:
                    try:
                        out = out.combine_first(s)
                    except Exception:
                        try:
                            out = out.where(out.notna(), s)
                        except Exception:
                            pass
            return out

        if isinstance(value, pd.Series):
            return value

        return pd.Series(value, index=df.index)
    except Exception:
        logger.exception("[summary.guards] safe_get_series failed col=%s", col)
        return None


def coerce_datetime_series(s: Optional[pd.Series]) -> pd.Series:
    try:
        if s is None:
            return pd.Series(dtype="datetime64[ns]")
        out = pd.to_datetime(s, errors="coerce")
        try:
            if getattr(out.dt, "tz", None) is not None:
                out = out.dt.tz_localize(None)
        except Exception:
            pass
        return out
    except Exception:
        logger.debug("[summary.guards] coerce datetime failed", exc_info=True)
        return pd.Series(dtype="datetime64[ns]")


def normalize_datetime_columns(df: pd.DataFrame, interval: int = 1) -> pd.DataFrame:
    """
    軽量版 normalize。
    scheduler側 guard 用として最低限だけ持つ。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    try:
        if "datetime" in out.columns:
            out["datetime"] = coerce_datetime_series(safe_get_series(out, "datetime"))
        else:
            date_col = "date" if "date" in out.columns else None
            time_col = None
            for c in ("time", "end_time", "start_time"):
                if c in out.columns:
                    time_col = c
                    break

            if date_col and time_col:
                out["datetime"] = pd.to_datetime(
                    safe_get_series(out, date_col).astype(str) + " " + safe_get_series(out, time_col).astype(str),
                    errors="coerce",
                )
            elif time_col:
                out["datetime"] = pd.to_datetime(safe_get_series(out, time_col), errors="coerce")
            else:
                out["datetime"] = pd.NaT
    except Exception:
        logger.debug("[summary.guards] normalize datetime failed", exc_info=True)

    try:
        if "date" not in out.columns and "datetime" in out.columns:
            out["date"] = pd.to_datetime(safe_get_series(out, "datetime"), errors="coerce").dt.strftime("%Y-%m-%d")
    except Exception:
        logger.debug("[summary.guards] normalize date failed", exc_info=True)

    return out


# ============================================================
# date extraction helpers
# ============================================================

def extract_actual_dates_from_df(df: pd.DataFrame) -> set:
    """
    入力DFに実際に入っている日付を抽出する。
    """
    try:
        if df is None or df.empty:
            return set()

        if "datetime" in df.columns:
            s = pd.to_datetime(safe_get_series(df, "datetime"), errors="coerce")
            vals = {x.date() for x in s.dropna()}
            if vals:
                return vals

        if "date" in df.columns:
            s = pd.to_datetime(safe_get_series(df, "date"), errors="coerce")
            vals = {x.date() for x in s.dropna()}
            if vals:
                return vals

        for c in ("end_time", "start_time", "time"):
            if c in df.columns:
                s = pd.to_datetime(safe_get_series(df, c), errors="coerce")
                vals = {x.date() for x in s.dropna()}
                if vals:
                    return vals
    except Exception:
        logger.debug("[summary.guards] extract actual dates failed", exc_info=True)

    return set()


def extract_dates_from_datetime_like(series: pd.Series) -> list:
    try:
        s = pd.to_datetime(series, errors="coerce")
        s = s.dropna()
        if s.empty:
            return []
        return sorted({x.date() for x in s})
    except Exception:
        logger.debug("[summary.guards] extract dates failed", exc_info=True)
        return []


# ============================================================
# guard helpers
# ============================================================

def drop_rows_outside_allowed_dates(
    df: pd.DataFrame,
    *,
    label: str,
    include_previous_business_day: bool = True,
    interval: int = 1,
) -> pd.DataFrame:
    """
    営業日ベースの許容日付でフィルタする。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    out = normalize_datetime_columns(out, interval=interval)
    if "datetime" not in out.columns:
        logger.warning("[summary.guards] %s skip date guard: datetime missing", label)
        return out

    dt_s = pd.to_datetime(safe_get_series(out, "datetime"), errors="coerce")
    if dt_s is None:
        return out

    row_dates = dt_s.dt.date
    fallback_dates = set(
        get_closed_day_allowed_dates(
            include_previous_business_day=include_previous_business_day
        )
    )
    actual_dates = extract_actual_dates_from_df(out)

    # actual_dates 優先ではなく営業日判定ベース優先
    allowed_dates = fallback_dates if fallback_dates else actual_dates

    keep_mask = row_dates.isin(allowed_dates)

    before = len(out)
    dropped = int((~keep_mask.fillna(False)).sum())

    if dropped > 0:
        sample_cols = [
            c for c in [
                "symbol", "symbolname", "datetime", "date", "time",
                "start_time", "end_time", "time_range",
                "open", "high", "low", "close", "source",
            ] if c in out.columns
        ]
        logger.warning(
            "[summary.guards] %s date guard dropped=%d before=%d allowed_dates=%s actual_dates=%s sample=\n%s",
            label,
            dropped,
            before,
            sorted(str(x) for x in allowed_dates),
            sorted(str(x) for x in actual_dates),
            out.loc[~keep_mask.fillna(False), sample_cols].head(20).to_string(index=False)
            if sample_cols else "(no sample cols)",
        )

    out = out.loc[keep_mask.fillna(False)].copy().reset_index(drop=True)
    logger.info(
        "[summary.guards] %s date guard rows=%d -> %d allowed_dates=%s actual_dates=%s",
        label,
        before,
        len(out),
        sorted(str(x) for x in allowed_dates),
        sorted(str(x) for x in actual_dates),
    )
    return out


def drop_rows_to_explicit_dates(
    df: pd.DataFrame,
    *,
    allowed_dates: Iterable,
    label: str,
    interval: int = 1,
) -> pd.DataFrame:
    """
    明示された日付集合だけ残す。
    delta push / rebuilt 分の guard 向け。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    out = normalize_datetime_columns(out, interval=interval)
    if "datetime" not in out.columns:
        logger.warning("[summary.guards] %s explicit date guard skipped: datetime missing", label)
        return out

    dt_s = pd.to_datetime(safe_get_series(out, "datetime"), errors="coerce")
    if dt_s is None:
        return out

    allowed = {pd.to_datetime(x).date() for x in allowed_dates if x is not None}
    if not allowed:
        logger.info("[summary.guards] %s explicit date guard skipped: no allowed dates", label)
        return out

    row_dates = dt_s.dt.date
    keep_mask = row_dates.isin(allowed)

    before = len(out)
    dropped = int((~keep_mask.fillna(False)).sum())

    if dropped > 0:
        sample_cols = [
            c for c in [
                "symbol", "symbolname", "datetime", "date", "time",
                "start_time", "end_time", "time_range",
                "open", "high", "low", "close", "source",
            ] if c in out.columns
        ]
        logger.warning(
            "[summary.guards] %s explicit date guard dropped=%d before=%d allowed_dates=%s sample=\n%s",
            label,
            dropped,
            before,
            sorted(str(x) for x in allowed),
            out.loc[~keep_mask.fillna(False), sample_cols].head(20).to_string(index=False)
            if sample_cols else "(no sample cols)",
        )

    out = out.loc[keep_mask.fillna(False)].copy().reset_index(drop=True)
    logger.info(
        "[summary.guards] %s explicit date guard rows=%d -> %d allowed_dates=%s",
        label,
        before,
        len(out),
        sorted(str(x) for x in allowed),
    )
    return out


def filter_latest_per_symbol(df: pd.DataFrame, *, datetime_col: str = "datetime") -> pd.DataFrame:
    """
    symbol ごとに最新1件へ絞る簡易 helper。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    if "symbol" not in out.columns or datetime_col not in out.columns:
        return out

    try:
        out[datetime_col] = pd.to_datetime(safe_get_series(out, datetime_col), errors="coerce")
        out = (
            out.sort_values(["symbol", datetime_col], kind="stable")
            .groupby("symbol", group_keys=False)
            .tail(1)
            .reset_index(drop=True)
        )
        return out
    except Exception:
        logger.debug("[summary.guards] filter latest per symbol failed", exc_info=True)
        return out


__all__ = [
    "ensure_dataframe",
    "safe_get_series",
    "coerce_datetime_series",
    "normalize_datetime_columns",
    "extract_actual_dates_from_df",
    "extract_dates_from_datetime_like",
    "drop_rows_outside_allowed_dates",
    "drop_rows_to_explicit_dates",
    "filter_latest_per_symbol",
]