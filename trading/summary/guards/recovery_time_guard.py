# ============================================================
# File   : trading/summary/guards/recovery_time_guard.py
# Version: Ver1.0-PRODUCTION-RECOVERY-TIME-GUARD
# ------------------------------------------------------------
# ✔ recovery / bootstrap 由来の未来時刻を遮断
# ✔ 当日市場時間中の 15:30 先行混入を防止
# ✔ 1min / 3min / 5min 共通利用可能
# ✔ datetime / start_time / end_time / time を安全補正
# ✔ JST naive datetime 基準
# ✔ source=summary_recovery_push_* の保護に最適
# ✔ production safe
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# market session constants
# ============================================================

JST = "Asia/Tokyo"

AM_START = dt.time(9, 0)
AM_END = dt.time(11, 30)
PM_START = dt.time(12, 30)
PM_END = dt.time(15, 30)


# ============================================================
# datetime helpers
# ============================================================

def _now_jst_naive() -> dt.datetime:
    return dt.datetime.now()


def _floor_dt(ts: dt.datetime, interval_min: int = 1) -> dt.datetime:
    ts = ts.replace(second=0, microsecond=0)
    minute = (ts.minute // max(1, interval_min)) * max(1, interval_min)
    return ts.replace(minute=minute, second=0, microsecond=0)


def _to_naive_timestamp(v) -> pd.Timestamp:
    ts = pd.to_datetime(v, errors="coerce")
    if pd.isna(ts):
        return pd.NaT

    try:
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_convert(JST).tz_localize(None)
    except Exception:
        try:
            ts = ts.tz_localize(None)
        except Exception:
            pass

    return ts


def _series_to_naive_datetime(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s, errors="coerce")

    try:
        if getattr(out.dt, "tz", None) is not None:
            out = out.dt.tz_convert(JST).dt.tz_localize(None)
    except Exception:
        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass

    return out


def _normalize_time_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in ["datetime", "start_time", "end_time", "time"]:
        if col in out.columns:
            out[col] = _series_to_naive_datetime(out[col])

    return out


# ============================================================
# market calendar logic
# ============================================================

def _is_weekday(d: dt.date) -> bool:
    return d.weekday() < 5


def _same_day(a: Optional[dt.datetime], b: Optional[dt.datetime]) -> bool:
    if a is None or b is None:
        return False
    return a.date() == b.date()


def _session_cap_for_now(now_dt: Optional[dt.datetime] = None) -> dt.datetime:
    """
    市場時間中の recovery が未来を作らないための上限時刻を返す。
    - 08:59以前  -> 当日 09:00 未満のため現在時刻floor
    - 09:00-11:30 -> 現在時刻floor
    - 11:30-12:30 -> 11:30
    - 12:30-15:30 -> 現在時刻floor
    - 15:30以降   -> 15:30
    """
    now_dt = now_dt or _now_jst_naive()
    now_floor = _floor_dt(now_dt, 1)
    t = now_floor.time()

    if t < AM_START:
        return now_floor

    if AM_START <= t <= AM_END:
        return now_floor

    if AM_END < t < PM_START:
        return now_floor.replace(hour=11, minute=30, second=0, microsecond=0)

    if PM_START <= t <= PM_END:
        return now_floor

    return now_floor.replace(hour=15, minute=30, second=0, microsecond=0)


def _clip_ts_to_market_cap(ts: pd.Timestamp, cap_dt: dt.datetime) -> pd.Timestamp:
    if pd.isna(ts):
        return ts
    if ts.to_pydatetime() > cap_dt:
        return pd.Timestamp(cap_dt)
    return ts


# ============================================================
# row / dataframe sanitizers
# ============================================================

def _rebuild_time_range(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "start_time" not in out.columns and "datetime" in out.columns:
        out["start_time"] = out["datetime"]

    if "end_time" not in out.columns and "datetime" in out.columns:
        out["end_time"] = out["datetime"]

    if "time" not in out.columns and "datetime" in out.columns:
        out["time"] = out["datetime"]

    if "start_time" in out.columns and "end_time" in out.columns:
        try:
            st = pd.to_datetime(out["start_time"], errors="coerce")
            ed = pd.to_datetime(out["end_time"], errors="coerce")

            out["time_range"] = (
                st.dt.strftime("%H:%M") + "-" + ed.dt.strftime("%H:%M")
            )
        except Exception:
            logger.exception("[RECOVERY TIME GUARD] rebuild time_range failed")

    return out


def _ensure_date_column(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "date" not in out.columns:
        if "datetime" in out.columns:
            try:
                out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.date.astype(str)
            except Exception:
                pass

    return out


def _drop_future_rows(
    df: pd.DataFrame,
    *,
    interval_min: int = 1,
    source_name: str = "",
    now_dt: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """
    当日分について、現在未到達の datetime を削除する。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = _normalize_time_columns(out)

    if "datetime" not in out.columns:
        return out

    now_dt = now_dt or _now_jst_naive()
    cap_dt = _session_cap_for_now(now_dt)
    cap_dt = _floor_dt(cap_dt, max(1, interval_min))
    today = now_dt.date()

    before = len(out)

    dt_series = pd.to_datetime(out["datetime"], errors="coerce")
    same_day_mask = dt_series.dt.date.eq(today)
    future_mask = same_day_mask & dt_series.gt(pd.Timestamp(cap_dt))

    if future_mask.any():
        logger.warning(
            "[RECOVERY TIME GUARD] drop future rows source=%s interval=%s cap=%s rows=%d/%d",
            source_name,
            interval_min,
            cap_dt,
            int(future_mask.sum()),
            before,
        )
        out = out.loc[~future_mask].copy()

    return out


def _clip_future_columns(
    df: pd.DataFrame,
    *,
    interval_min: int = 1,
    source_name: str = "",
    now_dt: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """
    残した行の time系列も cap 以下へ補正する。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = _normalize_time_columns(out)

    now_dt = now_dt or _now_jst_naive()
    cap_dt = _session_cap_for_now(now_dt)
    cap_dt = _floor_dt(cap_dt, max(1, interval_min))
    today = now_dt.date()

    for col in ["datetime", "start_time", "end_time", "time"]:
        if col not in out.columns:
            continue

        try:
            s = pd.to_datetime(out[col], errors="coerce")
            mask = s.dt.date.eq(today)
            if mask.any():
                out.loc[mask, col] = s.loc[mask].apply(lambda x: _clip_ts_to_market_cap(x, cap_dt))
        except Exception:
            logger.exception(
                "[RECOVERY TIME GUARD] clip column failed source=%s col=%s",
                source_name,
                col,
            )

    return out


def _dedup_by_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    keys = [c for c in ["symbol", "datetime"] if c in out.columns]
    if len(keys) == 2:
        try:
            out = out.sort_values(["symbol", "datetime"])
            out = out.drop_duplicates(subset=keys, keep="last")
        except Exception:
            logger.exception("[RECOVERY TIME GUARD] dedup failed")

    return out


# ============================================================
# public api
# ============================================================

def sanitize_recovery_summary_df(
    df: pd.DataFrame,
    *,
    interval_min: int = 1,
    source_name: str = "",
    now_dt: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """
    recovery / bootstrap 後、DB保存前に必ず通す。

    目的:
    - 当日市場時間中に未来時刻(例: 15:30)が混入しないようにする
    - 15:30-15:30 の擬似バーが先行して latest 扱いされるのを防ぐ
    """

    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    before = len(out)

    out = _normalize_time_columns(out)
    out = _drop_future_rows(
        out,
        interval_min=interval_min,
        source_name=source_name,
        now_dt=now_dt,
    )

    if out.empty:
        logger.warning(
            "[RECOVERY TIME GUARD] all rows dropped source=%s interval=%s",
            source_name,
            interval_min,
        )
        return out

    out = _clip_future_columns(
        out,
        interval_min=interval_min,
        source_name=source_name,
        now_dt=now_dt,
    )

    out = _rebuild_time_range(out)
    out = _ensure_date_column(out)
    out = _dedup_by_symbol_datetime(out)

    logger.info(
        "[RECOVERY TIME GUARD] sanitized source=%s interval=%s rows=%d -> %d",
        source_name,
        interval_min,
        before,
        len(out),
    )

    return out