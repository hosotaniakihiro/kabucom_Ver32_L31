# ============================================================
# File   : trading/summary/recovery/market_hours.py
# Ver    : PRODUCTION-STABLE-REV1.3-SUMMARY-RECOVERY-MARKET-HOURS-RAW-ONLY
# ------------------------------------------------------------
# 【概要】
#   summary recovery 用の市場時間フィルタ
#
# 【主な機能】
#   - interval ごとの許可終了時刻
#   - 市場時間内判定
#   - 営業日判定（土日・祝日除外）
#   - 市場時間外 row の除外
#
# 【今回の主修正】
#   - finalize_for_upsert 依存を除去
#   - raw整形の責務に限定
#   - keep_mask を「valid & business_day & market_session」に維持
#
# 【依存方針】
#   - guards の定数/補助関数を利用
#   - business_day_utils を利用
#   - preloaders / incremental_processors には依存しない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from trading.summary.recovery.helpers import normalize_datetime_columns
from trading.summary.recovery.guards import (
    AM_START,
    AM_END,
    PM_START,
    normalize_time_cols_for_guard,
    rebuild_time_range_from_cols,
)

logger = logging.getLogger(__name__)


# ============================================================
# business day helpers
# ============================================================

def _is_business_day_date(value) -> bool:
    """
    value に含まれる日付が営業日かを返す。
    - 土日を除外
    - 祝日CSV / business_day_utils が使える場合は祝日も除外
    - 判定失敗時は最低限、土日除外を適用
    """
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return False

        d = ts.date()

        # Monday=0 ... Sunday=6
        if d.weekday() >= 5:
            return False

        try:
            from utils.business_day_utils import is_business_day
            return bool(is_business_day(d))
        except Exception:
            logger.debug(
                "[summary_recovery.market_hours] business_day_utils unavailable -> weekend-only fallback date=%s",
                d,
            )
            return True

    except Exception:
        logger.exception(
            "[summary_recovery.market_hours] business day check failed value=%s",
            value,
        )
        return False


# ============================================================
# market session helpers
# ============================================================

def market_close_time_for_interval(interval: int) -> dt.time:
    interval = int(interval)

    if interval == 3:
        return dt.time(15, 33)

    if interval == 5:
        return dt.time(15, 35)

    return dt.time(15, 30)


def is_market_session_time(value, interval: int) -> bool:
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return False

        t = ts.time()
        pm_end = market_close_time_for_interval(interval)

        in_am = AM_START <= t <= AM_END
        in_pm = PM_START <= t <= pm_end

        return bool(in_am or in_pm)

    except Exception:
        return False


# ============================================================
# combined filter
# ============================================================

def filter_market_hours_rows(
    df: pd.DataFrame,
    interval: int,
    *,
    label: str,
) -> pd.DataFrame:
    """
    市場時間外データを除外する。
    さらに営業日でない日（土日・祝日）も除外する。

    許可時間:
      1min -> 09:00-11:30, 12:30-15:30
      3min -> 09:00-11:30, 12:30-15:33
      5min -> 09:00-11:30, 12:30-15:35

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

    try:
        dt_series = pd.to_datetime(out["datetime"], errors="coerce")

        valid_mask = dt_series.notna()
        business_mask = dt_series.apply(_is_business_day_date)
        session_mask = dt_series.apply(lambda x: is_market_session_time(x, int(interval)))

        keep_mask = valid_mask & business_mask & session_mask

        dropped = int((~keep_mask).sum())
        before = len(out)

        out = out.loc[keep_mask].copy()

        logger.info(
            "[summary_recovery.market_hours] market-hours filter label=%s interval=%s before=%d after=%d dropped=%d pm_end=%s",
            label,
            interval,
            before,
            len(out),
            dropped,
            market_close_time_for_interval(int(interval)),
        )

        if out.empty:
            return out

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
            "[summary_recovery.market_hours] market-hours filter failed label=%s interval=%s",
            label,
            interval,
        )
        return out


__all__ = [
    "market_close_time_for_interval",
    "is_market_session_time",
    "filter_market_hours_rows",
]