# ============================================================
# File   : trading/yahoo/pipeline/complement/diff.py
# Version: PRODUCTION-STABLE-REV4.2.1-YAHOO-COMPLEMENT-DIFF-FIX
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完の差分計算ユーティリティ
#
# 【主な機能】
#   - latest_yahoo_dt から warmup 読み込み開始時刻を計算
#   - latest_yahoo_dt から overlap 分戻した保存開始時刻を計算
#   - 計算用DataFrameは warmup込みで保持
#   - 保存用DataFrameだけ差分抽出
#
# 【今回の修正】
#   - runner.py が import する以下の関数を確実に定義:
#       calc_fetch_start
#       filter_from_fetch_start
#       filter_diff_rows
#
# 【重要】
#   - 差分基準は PUSH由来ではなく Yahoo由来 source の最新時刻
#   - indicator/scoring の前に保存差分で絞らない
#   - filter_diff_rows() は保存直前だけで使う
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

try:
    from .constants import (
        DEFAULT_WARMUP_MINUTES,
        DEFAULT_OVERLAP_MINUTES_BY_INTERVAL,
    )
except Exception:  # pragma: no cover
    DEFAULT_WARMUP_MINUTES = 140
    DEFAULT_OVERLAP_MINUTES_BY_INTERVAL = {
        1: 5,
        3: 15,
        5: 25,
    }

try:
    from .normalize import safe_df, normalize_datetime_df
except Exception:  # pragma: no cover
    def safe_df(df):
        try:
            if df is None:
                return pd.DataFrame()
            if isinstance(df, pd.DataFrame):
                out = df.copy()
            else:
                out = pd.DataFrame(df)
            if out.empty:
                return pd.DataFrame()
            try:
                out = out.loc[:, ~out.columns.duplicated()]
            except Exception:
                pass
            return out
        except Exception:
            return pd.DataFrame()

    def normalize_datetime_df(df):
        out = safe_df(df)
        if out.empty:
            return out
        if "datetime" not in out.columns:
            return pd.DataFrame()
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"])
        if out.empty:
            return pd.DataFrame()
        try:
            if getattr(out["datetime"].dt, "tz", None) is not None:
                try:
                    out["datetime"] = out["datetime"].dt.tz_convert(None)
                except Exception:
                    out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        out["datetime"] = out["datetime"].dt.floor("min")
        return out


logger = logging.getLogger(__name__)


# ============================================================
# timestamp helpers
# ============================================================

def _to_timestamp(value) -> Optional[pd.Timestamp]:
    try:
        if value is None:
            return None

        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None

        ts = pd.Timestamp(ts)

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


def _default_market_fetch_start(now: Optional[dt.datetime] = None) -> pd.Timestamp:
    """
    Yahoo補完の初回実行時のデフォルト開始時刻。
    当日 08:30 から読む。
    """
    n = now or dt.datetime.now()
    return pd.Timestamp(n.strftime("%Y-%m-%d") + " 08:30:00")


# ============================================================
# public: fetch start
# ============================================================

def calc_fetch_start(
    latest_yahoo_dt: Optional[pd.Timestamp],
    *,
    warmup_minutes: int = DEFAULT_WARMUP_MINUTES,
    now: Optional[dt.datetime] = None,
) -> pd.Timestamp:
    """
    計算用の読み込み開始時刻を返す。

    latest_yahoo_dt がある場合:
        latest_yahoo_dt - warmup_minutes

    latest_yahoo_dt がない場合:
        当日 08:30

    注意:
        この関数は「保存開始時刻」ではない。
        indicator/scoring用の計算開始時刻。
    """
    latest = _to_timestamp(latest_yahoo_dt)

    if latest is not None:
        fetch_start = latest - pd.Timedelta(minutes=int(warmup_minutes))
    else:
        fetch_start = _default_market_fetch_start(now)

    logger.info(
        "[YAHOO DIFF] calc_fetch_start latest_yahoo_dt=%s warmup_minutes=%s fetch_start=%s",
        latest,
        warmup_minutes,
        fetch_start,
    )

    return fetch_start


# 互換alias
calculate_fetch_start = calc_fetch_start
get_fetch_start = calc_fetch_start


# ============================================================
# public: save start
# ============================================================

def calc_save_from(
    latest_yahoo_dt: Optional[pd.Timestamp],
    *,
    interval: int,
    overlap_minutes_by_interval: Optional[dict[int, int]] = None,
) -> Optional[pd.Timestamp]:
    """
    保存用の開始時刻を返す。

    latest_yahoo_dt がある場合:
        latest_yahoo_dt - overlap_minutes

    latest_yahoo_dt がない場合:
        None = 全保存
    """
    latest = _to_timestamp(latest_yahoo_dt)
    if latest is None:
        logger.info(
            "[YAHOO DIFF] calc_save_from interval=%s latest_yahoo_dt=None -> full save",
            interval,
        )
        return None

    overlap_map = overlap_minutes_by_interval or DEFAULT_OVERLAP_MINUTES_BY_INTERVAL
    overlap = int(overlap_map.get(int(interval), int(interval) * 5))

    save_from = latest - pd.Timedelta(minutes=overlap)

    logger.info(
        "[YAHOO DIFF] calc_save_from interval=%s latest_yahoo_dt=%s overlap=%s save_from=%s",
        interval,
        latest,
        overlap,
        save_from,
    )

    return save_from


# 互換alias
calculate_save_from = calc_save_from
get_save_from = calc_save_from


# ============================================================
# public: filters
# ============================================================

def filter_from_fetch_start(
    df: pd.DataFrame,
    *,
    fetch_start: Optional[pd.Timestamp],
) -> pd.DataFrame:
    """
    計算用DataFrameを fetch_start 以降に絞る。

    重要:
        これは warmup込みの計算範囲を作るためのフィルタ。
        保存差分フィルタではない。
    """
    out = safe_df(df)
    if out.empty:
        return out

    out = normalize_datetime_df(out)
    if out.empty:
        return out

    start = _to_timestamp(fetch_start)
    if start is None:
        return out

    before = len(out)
    out = out[out["datetime"] >= start].copy()

    logger.info(
        "[YAHOO DIFF] fetch_start filter rows=%s -> %s fetch_start=%s dt_min=%s dt_max=%s",
        before,
        len(out),
        start,
        out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
        out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
    )

    return out


def filter_diff_rows(
    df: pd.DataFrame,
    *,
    interval: int,
    latest_yahoo_dt: Optional[pd.Timestamp],
    overlap_minutes_by_interval: Optional[dict[int, int]] = None,
) -> pd.DataFrame:
    """
    保存対象の差分行を抽出する。

    重要:
        この関数は indicator/scoring の後、保存直前だけで使う。
        計算前に使うと 3分/5分足の履歴が不足し、
        RSI / MACD / slope が計算できなくなる。
    """
    out = safe_df(df)
    if out.empty:
        return out

    out = normalize_datetime_df(out)
    if out.empty:
        return out

    save_from = calc_save_from(
        latest_yahoo_dt,
        interval=int(interval),
        overlap_minutes_by_interval=overlap_minutes_by_interval,
    )

    before = len(out)

    if save_from is not None:
        out = out[out["datetime"] >= save_from].copy()

    logger.info(
        "[YAHOO DIFF] save diff filter interval=%s latest_yahoo_dt=%s save_from=%s rows=%s -> %s dt_min=%s dt_max=%s",
        interval,
        latest_yahoo_dt,
        save_from,
        before,
        len(out),
        out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
        out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
    )

    return out


# 互換alias
filter_save_diff_rows = filter_diff_rows
filter_diff_for_save = filter_diff_rows


# ============================================================
# diagnostics
# ============================================================

def summarize_diff_window(
    *,
    interval: int,
    latest_yahoo_dt: Optional[pd.Timestamp],
    warmup_minutes: int = DEFAULT_WARMUP_MINUTES,
    overlap_minutes_by_interval: Optional[dict[int, int]] = None,
    now: Optional[dt.datetime] = None,
) -> dict[str, Optional[pd.Timestamp]]:
    """
    診断用。
    fetch_start と save_from をまとめて返す。
    """
    fetch_start = calc_fetch_start(
        latest_yahoo_dt,
        warmup_minutes=warmup_minutes,
        now=now,
    )
    save_from = calc_save_from(
        latest_yahoo_dt,
        interval=int(interval),
        overlap_minutes_by_interval=overlap_minutes_by_interval,
    )

    logger.info(
        "[YAHOO DIFF] window interval=%s latest_yahoo_dt=%s fetch_start=%s save_from=%s",
        interval,
        latest_yahoo_dt,
        fetch_start,
        save_from,
    )

    return {
        "latest_yahoo_dt": _to_timestamp(latest_yahoo_dt),
        "fetch_start": fetch_start,
        "save_from": save_from,
    }


__all__ = [
    "calc_fetch_start",
    "calculate_fetch_start",
    "get_fetch_start",
    "calc_save_from",
    "calculate_save_from",
    "get_save_from",
    "filter_from_fetch_start",
    "filter_diff_rows",
    "filter_save_diff_rows",
    "filter_diff_for_save",
    "summarize_diff_window",
]