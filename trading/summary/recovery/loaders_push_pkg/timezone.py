# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/timezone.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-TIMEZONE
# ------------------------------------------------------------
# 【概要】
#   PUSH時刻の timezone 正規化
#
# 【主な機能】
#   ✔ tz-aware / tz-naive 混在対策
#   ✔ JST壁時計時刻を保持したまま tz だけ外す
#   ✔ UTC変換による9時間ズレを防止
#   ✔ SQL WHERE 用 datetime 文字列化
#
# 【重要】
#   - tz_convert(None) は使わない
#   - 2026-04-20 10:37:50+09:00
#       → 2026-04-20 10:37:50
#   - UTC変換しない
# ============================================================

from __future__ import annotations

import logging
import warnings
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def strip_tz_keep_wallclock(v):
    """
    timezone付き datetime を UTC変換せず、壁時計時刻を維持して tz だけ外す。

    例:
      2026-04-20 10:37:50+09:00
          -> 2026-04-20 10:37:50

    NG:
      2026-04-20 10:37:50+09:00
          -> 2026-04-20 01:37:50
    """
    try:
        if v is None:
            return pd.NaT

        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() in {"nan", "none", "nat", "<na>", "null"}:
                return pd.NaT
            v = s

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            ts = pd.Timestamp(v)

        if pd.isna(ts):
            return pd.NaT

        try:
            if ts.tzinfo is not None:
                ts = ts.tz_localize(None)
        except Exception:
            try:
                ts = pd.Timestamp(ts.replace(tzinfo=None))
            except Exception:
                pass

        return pd.Timestamp(ts)

    except Exception:
        return pd.NaT


def to_tz_naive_timestamp(v, *, label: str = "") -> Optional[pd.Timestamp]:
    """
    任意の datetime-like 値を timezone なし Timestamp に統一する。
    UTC変換はせず、壁時計時刻を保持する。
    """
    try:
        ts = strip_tz_keep_wallclock(v)
        if ts is None or pd.isna(ts):
            return None
        return pd.Timestamp(ts)
    except Exception:
        logger.debug(
            "[summary.recovery.loaders_push.timezone] to_tz_naive_timestamp failed label=%s value=%r",
            label,
            v,
            exc_info=True,
        )
        return None


def to_tz_naive_datetime_series(s, *, label: str = "") -> pd.Series:
    """
    Series / array-like を timezone なし datetime64[ns] に統一する。
    UTC変換はせず、JST壁時計時刻を保持する。

    対応:
      - 2026-04-20 10:37:50+09:00
      - 2026-04-20 10:37:50
      - datetime64[ns]
      - datetime64[ns, Asia/Tokyo]
      - mixed object
    """
    try:
        if s is None:
            return pd.Series(dtype="datetime64[ns]")

        if isinstance(s, pd.DataFrame):
            if s.shape[1] <= 0:
                return pd.Series(dtype="datetime64[ns]")
            s = s.iloc[:, 0]

        if not isinstance(s, pd.Series):
            s = pd.Series(s)

        if pd.api.types.is_datetime64_any_dtype(s) and not pd.api.types.is_datetime64tz_dtype(s):
            out = pd.to_datetime(s, errors="coerce")
            try:
                out = out.dt.tz_localize(None)
            except Exception:
                pass
            return out

        out = s.map(strip_tz_keep_wallclock)
        out = pd.to_datetime(out, errors="coerce")

        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_push.timezone] to_tz_naive_datetime_series failed label=%s",
            label,
        )
        try:
            return pd.Series(pd.NaT, index=getattr(s, "index", None), dtype="datetime64[ns]")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


def format_sql_dt(v, *, label: str = "") -> Optional[str]:
    """
    SQL WHERE パラメータ用に tz-naive の文字列へ変換する。
    """
    ts = to_tz_naive_timestamp(v, label=label)
    if ts is None or pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------
# Backward-compatible aliases
# ------------------------------------------------------------
_strip_tz_keep_wallclock = strip_tz_keep_wallclock
_to_tz_naive_timestamp = to_tz_naive_timestamp
_to_tz_naive_datetime_series = to_tz_naive_datetime_series
_format_sql_dt = format_sql_dt


__all__ = [
    "strip_tz_keep_wallclock",
    "to_tz_naive_timestamp",
    "to_tz_naive_datetime_series",
    "format_sql_dt",
    "_strip_tz_keep_wallclock",
    "_to_tz_naive_timestamp",
    "_to_tz_naive_datetime_series",
    "_format_sql_dt",
]