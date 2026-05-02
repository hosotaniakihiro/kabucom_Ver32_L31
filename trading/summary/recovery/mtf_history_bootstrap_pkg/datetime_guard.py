# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/datetime_guard.py
# Version: PRODUCTION-STABLE-REV1.0-DATETIME-FUTURE-GUARD
# ------------------------------------------------------------
# 【概要】
#   datetime 正規化 / 未来足除外ガード
#
# 【主な機能】
#   ✔ timezone付き datetime を壁時計時刻のまま naive 化
#   ✔ date + start_time / time / time_range / end_time から datetime 復元
#   ✔ 未来足 cutoff を現在時刻から計算
#   ✔ 当日分のみ未来足を drop
#   ✔ 前営業日データは 15:30 まで保持
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import warnings
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# local df helper
# ============================================================

def _ensure_df_local(df: Any) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            out = df.copy()
        else:
            out = pd.DataFrame(df)

        if out.empty:
            return pd.DataFrame()

        out.columns = [str(c) for c in out.columns]

        if out.columns.duplicated().any():
            dup = out.columns[out.columns.duplicated()].tolist()
            logger.warning("[MTF HISTORY BOOTSTRAP] duplicate columns removed=%s", dup)
            out = out.loc[:, ~out.columns.duplicated(keep="last")].copy()

        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[MTF HISTORY BOOTSTRAP] ensure df failed")
        return pd.DataFrame()


# ============================================================
# datetime parse
# ============================================================

def _strip_tz_keep_wallclock(v: Any):
    """
    timezone付き datetime を UTC変換せず、壁時計時刻を維持して tz だけ外す。

    例:
      2026-04-20 13:20:00+09:00
        -> 2026-04-20 13:20:00
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

        if ts.tzinfo is not None:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                try:
                    ts = pd.Timestamp(ts.replace(tzinfo=None))
                except Exception:
                    pass

        return pd.Timestamp(ts)

    except Exception:
        return pd.NaT


def _safe_to_datetime_naive_series(s: Any) -> pd.Series:
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

        out = s.map(_strip_tz_keep_wallclock)
        out = pd.to_datetime(out, errors="coerce")

        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] datetime parse failed", exc_info=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                return pd.to_datetime(pd.Series(s), errors="coerce")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


def _first_time_from_time_range(s: pd.Series) -> pd.Series:
    try:
        return (
            s.astype(str)
            .str.strip()
            .str.replace("〜", "-", regex=False)
            .str.replace("～", "-", regex=False)
            .str.split("-", n=1, expand=True)[0]
            .str.strip()
        )
    except Exception:
        return pd.Series("", index=s.index)


def _normalize_time_string_series(s: pd.Series) -> pd.Series:
    """
    time / start_time / end_time / time_range 由来の値を HH:MM:SS へ寄せる。
    """
    try:
        raw = s.astype(str).str.strip()
        raw = raw.replace(
            {
                "": np.nan,
                "nan": np.nan,
                "NaN": np.nan,
                "None": np.nan,
                "none": np.nan,
                "NaT": np.nan,
                "<NA>": np.nan,
            }
        )
        return raw
    except Exception:
        return pd.Series(np.nan, index=s.index)


# ============================================================
# cutoff
# ============================================================

def runtime_cutoff_now() -> pd.Timestamp:
    """
    現在時刻から見て、当日リアルタイム処理で許可する最大 datetime。

    優先:
      utils.market_time.get_intraday_cutoff_datetime

    fallback:
      09:00前       now
      09:00-11:30  now
      11:30-12:30  11:30
      12:30-15:30  now
      15:30後       15:30
    """
    now = pd.Timestamp.now().replace(second=0, microsecond=0)

    try:
        from utils.market_time import get_intraday_cutoff_datetime

        cutoff = get_intraday_cutoff_datetime(now.to_pydatetime())
        ts = pd.Timestamp(cutoff).replace(second=0, microsecond=0)
        try:
            if ts.tzinfo is not None:
                ts = ts.tz_localize(None)
        except Exception:
            pass
        return ts

    except Exception:
        logger.debug("[MTF FUTURE GUARD] market_time cutoff unavailable -> fallback", exc_info=True)

    d = now.date()
    t = now.time()

    if t < dt.time(9, 0):
        return now

    if dt.time(9, 0) <= t <= dt.time(11, 30):
        return now

    if dt.time(11, 30) < t < dt.time(12, 30):
        return pd.Timestamp(dt.datetime.combine(d, dt.time(11, 30)))

    if dt.time(12, 30) <= t <= dt.time(15, 30):
        return now

    return pd.Timestamp(dt.datetime.combine(d, dt.time(15, 30)))


# ============================================================
# normalize / guards
# ============================================================

def normalize_higher_tf_datetime(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    1分足/3分足/5分足 DataFrame の datetime を保証する。

    優先順位:
      1. datetime が有効なら採用
      2. date + start_time
      3. date + time
      4. date + time_range の開始時刻
      5. date + end_time
      6. datetime が復元できない行は NaT のまま残し、呼び出し元で drop
    """
    out = _ensure_df_local(df)
    if out.empty:
        return out

    try:
        interval = int(interval)
    except Exception:
        interval = 1

    if "datetime" in out.columns:
        out["datetime"] = _safe_to_datetime_naive_series(out["datetime"])
    else:
        out["datetime"] = pd.NaT

    try:
        if out["datetime"].isna().any() and "date" in out.columns:
            date_s = out["date"].astype(str).str.strip()
            date_s = date_s.replace(
                {
                    "": np.nan,
                    "nan": np.nan,
                    "NaN": np.nan,
                    "None": np.nan,
                    "none": np.nan,
                    "NaT": np.nan,
                    "<NA>": np.nan,
                }
            )

            candidates: list[pd.Series] = []

            if "start_time" in out.columns:
                candidates.append(_normalize_time_string_series(out["start_time"]))

            if "time" in out.columns:
                candidates.append(_normalize_time_string_series(out["time"]))

            if "time_range" in out.columns:
                candidates.append(_normalize_time_string_series(_first_time_from_time_range(out["time_range"])))

            if "end_time" in out.columns:
                candidates.append(_normalize_time_string_series(out["end_time"]))

            for t_s in candidates:
                rebuilt = pd.to_datetime(date_s.astype(str) + " " + t_s.astype(str), errors="coerce")
                fill_mask = out["datetime"].isna() & rebuilt.notna()
                if fill_mask.any():
                    out.loc[fill_mask, "datetime"] = rebuilt.loc[fill_mask]

        out["datetime"] = _safe_to_datetime_naive_series(out["datetime"])

        valid = out["datetime"].notna()
        if valid.any():
            dtv = out.loc[valid, "datetime"]

            out.loc[valid, "date"] = dtv.dt.strftime("%Y-%m-%d")
            out.loc[valid, "time"] = dtv.dt.strftime("%H:%M:%S")

            start = dtv
            end = start + pd.to_timedelta(interval, unit="m")

            out.loc[valid, "start_time"] = start.dt.strftime("%H:%M:%S")
            out.loc[valid, "end_time"] = end.dt.strftime("%H:%M:%S")
            out.loc[valid, "time_range"] = start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")

        out["interval"] = interval

        if "source" not in out.columns:
            out["source"] = f"mtf_history_bootstrap_{interval}min"

        invalid_count = int(out["datetime"].isna().sum())
        if invalid_count:
            logger.warning(
                "[MTF HISTORY BOOTSTRAP] datetime normalize still invalid interval=%s invalid=%s total=%s",
                interval,
                invalid_count,
                len(out),
            )

        return out

    except Exception:
        logger.exception("[MTF HISTORY BOOTSTRAP] normalize_higher_tf_datetime failed interval=%s", interval)
        return out


def drop_invalid_datetime_rows(df: pd.DataFrame, *, interval: int, label: str) -> pd.DataFrame:
    out = _ensure_df_local(df)
    if out.empty:
        return out

    out = normalize_higher_tf_datetime(out, interval=int(interval))

    if "datetime" not in out.columns:
        logger.warning(
            "[MTF HISTORY BOOTSTRAP] %s datetime column missing interval=%s rows=%s -> empty",
            label,
            interval,
            len(out),
        )
        return pd.DataFrame()

    before = len(out)
    out = out.dropna(subset=["datetime"]).copy()
    dropped = before - len(out)

    if dropped:
        logger.warning(
            "[MTF HISTORY BOOTSTRAP] %s dropped invalid datetime interval=%s before=%s after=%s dropped=%s",
            label,
            interval,
            before,
            len(out),
            dropped,
        )

    return out.reset_index(drop=True)


def drop_future_datetime_rows(df: pd.DataFrame, *, interval: int, label: str) -> pd.DataFrame:
    """
    現在時刻から見て未来の足を落とす。

    重要:
      - 前営業日データは残す
      - 当日データだけ cutoff を適用
      - resample(label='right') で現在時刻より先のバーができる場合もここで落とす
    """
    out = drop_invalid_datetime_rows(df, interval=int(interval), label=f"{label}_invalid_guard")
    if out.empty:
        return out

    try:
        cutoff = runtime_cutoff_now()
        today = pd.Timestamp.now().date()

        dt_s = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            dt_s = dt_s.dt.tz_localize(None)
        except Exception:
            pass

        today_mask = dt_s.dt.date == today
        future_mask = today_mask & (dt_s > cutoff)

        future_rows = int(future_mask.sum())
        if future_rows <= 0:
            return out.reset_index(drop=True)

        before = len(out)
        before_max = dt_s.max()

        logger.warning(
            "[MTF FUTURE GUARD] %s interval=%s cutoff=%s before_rows=%s before_dt_max=%s future_rows=%s",
            label,
            interval,
            cutoff,
            before,
            before_max,
            future_rows,
        )

        out = out.loc[~future_mask].copy()

        logger.info(
            "[MTF FUTURE GUARD] %s interval=%s after_rows=%s removed=%s after_dt_max=%s",
            label,
            interval,
            len(out),
            before - len(out),
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        )

        return out.reset_index(drop=True)

    except Exception:
        logger.exception(
            "[MTF FUTURE GUARD] failed label=%s interval=%s -> keep invalid-filtered",
            label,
            interval,
        )
        return out.reset_index(drop=True)


__all__ = [
    "runtime_cutoff_now",
    "normalize_higher_tf_datetime",
    "drop_invalid_datetime_rows",
    "drop_future_datetime_rows",
]