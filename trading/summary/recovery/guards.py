# ============================================================
# File   : trading/summary/recovery/guards.py
# Ver    : PRODUCTION-STABLE-REV1.2-SUMMARY-RECOVERY-GUARDS-RAW-ONLY
# ------------------------------------------------------------
# 【概要】
#   summary recovery 用の時刻ガード群
#
# 【主な機能】
#   - JST naive 現在時刻取得
#   - interval floor
#   - datetime/start/end/time 正規化
#   - 当日 future row の drop / clip
#   - time_range / date の再構築
#
# 【今回の主修正】
#   - finalize_for_upsert 依存を除去
#   - raw整形の責務に限定
#   - guard 内で upsert/cache 用整形をしない
#
# 【依存方針】
#   - helpers.normalize_datetime_columns を利用
#   - market_hours / preloaders には依存しない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from trading.summary.recovery.helpers import normalize_datetime_columns

logger = logging.getLogger(__name__)

JST = "Asia/Tokyo"
AM_START = dt.time(9, 0)
AM_END = dt.time(11, 30)
PM_START = dt.time(12, 30)
PM_END = dt.time(15, 30)


def now_jst_naive() -> dt.datetime:
    return dt.datetime.now()


def floor_dt(ts: dt.datetime, interval_min: int = 1) -> dt.datetime:
    ts = ts.replace(second=0, microsecond=0)
    interval_min = max(int(interval_min), 1)
    minute = (ts.minute // interval_min) * interval_min
    return ts.replace(minute=minute, second=0, microsecond=0)


def normalize_dt_like(value):
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if ts is None or pd.isna(ts):
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
        return None


def normalize_time_cols_for_guard(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for col in ["datetime", "start_time", "end_time", "time"]:
        if col not in out.columns:
            continue
        try:
            s = pd.to_datetime(out[col], errors="coerce")
            try:
                if getattr(s.dt, "tz", None) is not None:
                    s = s.dt.tz_convert(JST).dt.tz_localize(None)
            except Exception:
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
            out[col] = s
        except Exception:
            logger.exception("[summary_recovery.guards] normalize time col failed col=%s", col)

    return out


def session_cap_for_now(now_dt: Optional[dt.datetime] = None) -> dt.datetime:
    now_dt = now_dt or now_jst_naive()
    now_floor = floor_dt(now_dt, 1)
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


def clip_ts_to_cap(ts, cap_dt: dt.datetime):
    try:
        if ts is None or pd.isna(ts):
            return ts
        ts = pd.to_datetime(ts, errors="coerce")
        if pd.isna(ts):
            return ts
        if ts.to_pydatetime() > cap_dt:
            return pd.Timestamp(cap_dt)
        return ts
    except Exception:
        return ts


def rebuild_time_range_from_cols(df: pd.DataFrame) -> pd.DataFrame:
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
            out["time_range"] = st.dt.strftime("%H:%M") + "-" + ed.dt.strftime("%H:%M")
        except Exception:
            logger.exception("[summary_recovery.guards] rebuild time_range failed")

    if "date" not in out.columns and "datetime" in out.columns:
        try:
            out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.date.astype(str)
        except Exception:
            logger.exception("[summary_recovery.guards] rebuild date failed")

    return out


def guard_future_rows(
    df: pd.DataFrame,
    interval: int,
    *,
    label: str,
    now_dt: Optional[dt.datetime] = None,
    skip_same_day_future_drop: bool = False,
) -> pd.DataFrame:
    """
    当日分の future row を drop / clip する。
    bootstrap_delta_1m など、起動高速化ルートでは same-day future drop を緩和できる。

    重要:
      - 本関数は raw 整形のみを担当する
      - finalize_for_upsert はここでは呼ばない
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = normalize_datetime_columns(out, interval=int(interval))
    out = normalize_time_cols_for_guard(out)

    if "datetime" not in out.columns:
        return out

    now_dt = now_dt or now_jst_naive()
    cap_dt = session_cap_for_now(now_dt)
    cap_dt = floor_dt(cap_dt, max(int(interval), 1))
    today = now_dt.date()

    try:
        dt_series = pd.to_datetime(out["datetime"], errors="coerce")
        same_day_mask = dt_series.dt.date.eq(today)

        if not skip_same_day_future_drop:
            future_mask = same_day_mask & dt_series.gt(pd.Timestamp(cap_dt))

            if future_mask.any():
                logger.warning(
                    "[summary_recovery.guards] future rows dropped label=%s interval=%s cap_dt=%s rows=%d/%d",
                    label,
                    interval,
                    cap_dt,
                    int(future_mask.sum()),
                    len(out),
                )
                out = out.loc[~future_mask].copy()
        else:
            future_mask = same_day_mask & dt_series.gt(pd.Timestamp(cap_dt))
            if future_mask.any():
                logger.info(
                    "[summary_recovery.guards] future rows kept (delta-only fast boot) label=%s interval=%s cap_dt=%s rows=%d/%d",
                    label,
                    interval,
                    cap_dt,
                    int(future_mask.sum()),
                    len(out),
                )

        if out.empty:
            return out

        for col in ["datetime", "start_time", "end_time", "time"]:
            if col not in out.columns:
                continue
            s = pd.to_datetime(out[col], errors="coerce")
            if skip_same_day_future_drop:
                continue
            mask = s.dt.date.eq(today)
            if mask.any():
                out.loc[mask, col] = s.loc[mask].apply(lambda x: clip_ts_to_cap(x, cap_dt))

        out = rebuild_time_range_from_cols(out)
        out = normalize_datetime_columns(out, interval=int(interval))

        if "symbol" in out.columns and "datetime" in out.columns:
            try:
                out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            except Exception:
                pass

            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = (
                out.dropna(subset=["symbol", "datetime"])
                .sort_values(["symbol", "datetime"])
                .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )

        return out

    except Exception:
        logger.exception(
            "[summary_recovery.guards] future row guard failed label=%s interval=%s",
            label,
            interval,
        )
        return out


__all__ = [
    "JST",
    "AM_START",
    "AM_END",
    "PM_START",
    "PM_END",
    "now_jst_naive",
    "floor_dt",
    "normalize_dt_like",
    "normalize_time_cols_for_guard",
    "session_cap_for_now",
    "clip_ts_to_cap",
    "rebuild_time_range_from_cols",
    "guard_future_rows",
]