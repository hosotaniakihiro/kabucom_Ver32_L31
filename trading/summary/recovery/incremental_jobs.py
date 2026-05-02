# ============================================================
# File   : trading/summary/recovery/incremental_jobs.py
# Ver    : PRODUCTION-STABLE-REV3-INCREMENTAL-JOBS-INDICATORS-SCORING
# ------------------------------------------------------------
# ✔ process_incremental_1m
# ✔ process_incremental_higher_tf
# ✔ update_global_cache の引数順修正
# ✔ DB保存用 raw と cache更新用 raw を明確化
# ✔ NEW: OHLCV再構築後に indicator 計算を追加
# ✔ NEW: indicator 後に scoring を追加
# ✔ NEW: 1m は existing_1m + delta を merge した最近履歴で再計算
# ✔ NEW: 3m/5m も resample 後に indicator/scoring を付与
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from trading.summary.recovery.helpers import (
    merge_summary_frames_with_priority,
    normalize_datetime_columns,
)
from trading.summary.recovery.persistence import (
    finalize_for_upsert,
    update_global_cache,
    upsert_summary_df,
)
from trading.summary.recovery.rebuilders import (
    RECENT_RECALC_BARS_1M,
    rebuild_1min_from_push,
    rebuild_higher_tf_from_1m,
    trim_recent_bars,
)
from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.scoring.core.scoring_core import scoring_main

logger = logging.getLogger(__name__)


def _interval_name(interval: int) -> str:
    try:
        interval = int(interval)
    except Exception:
        interval = 1

    if interval == 1:
        return "1min"
    if interval == 3:
        return "3min"
    if interval == 5:
        return "5min"
    return f"{interval}min"


def _apply_indicators_and_scoring(
    df: pd.DataFrame,
    *,
    interval: int,
) -> pd.DataFrame:
    """
    raw OHLCV に対して
      1) indicator 計算
      2) scoring 計算
    を適用する。

    重要:
      - cache/update 用 raw を返す
      - finalize_for_upsert はここでは呼ばない
    """
    try:
        out = normalize_datetime_columns(df, interval=int(interval))
        if out.empty:
            return out

        interval_name = _interval_name(int(interval))

        out = add_all_indicators(out, interval=interval_name)
        out = normalize_datetime_columns(out, interval=int(interval))

        if out.empty:
            logger.info(
                "[summary_recovery] indicators produced empty df interval=%s",
                interval_name,
            )
            return out

        out = scoring_main(out, interval=interval_name, force=True)
        out = normalize_datetime_columns(out, interval=int(interval))

        logger.info(
            "[summary_recovery] indicators+scoring done interval=%s rows=%d symbols=%d latest_dt=%s",
            interval_name,
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns and not out.empty else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        )
        return out

    except Exception:
        logger.exception(
            "[summary_recovery] indicators/scoring failed interval=%s",
            interval,
        )
        return normalize_datetime_columns(df, interval=int(interval))


def process_incremental_1m(
    df_push: pd.DataFrame,
    *,
    existing_1m: Optional[pd.DataFrame] = None,
    persist: bool = True,
    update_cache: bool = True,
) -> pd.DataFrame:
    try:
        delta_1m_ohlcv = rebuild_1min_from_push(df_push)
        delta_1m_ohlcv = normalize_datetime_columns(delta_1m_ohlcv, interval=1)

        if delta_1m_ohlcv.empty:
            logger.info("[summary_recovery] process_incremental_1m skipped: delta_1m empty")
            return pd.DataFrame()

        # 既存 recent history と delta を merge した上で
        # indicator/scoring を再計算する
        full_1m_raw = merge_summary_frames_with_priority(existing_1m, delta_1m_ohlcv, interval=1)
        full_1m_raw = normalize_datetime_columns(full_1m_raw, interval=1)
        full_1m_raw = trim_recent_bars(full_1m_raw, bars=RECENT_RECALC_BARS_1M)
        full_1m_raw = normalize_datetime_columns(full_1m_raw, interval=1)

        full_1m_raw = _apply_indicators_and_scoring(full_1m_raw, interval=1)
        full_1m_raw = normalize_datetime_columns(full_1m_raw, interval=1)

        if full_1m_raw.empty:
            logger.info("[summary_recovery] process_incremental_1m skipped: full_1m_raw empty after indicators/scoring")
            return pd.DataFrame()

        # DB保存用
        full_1m_upsert = finalize_for_upsert(full_1m_raw, 1)

        if persist and not full_1m_upsert.empty:
            upsert_summary_df(full_1m_upsert, 1)

        # cache更新用は raw を渡す
        if update_cache and not full_1m_raw.empty:
            update_global_cache(full_1m_raw, 1)

        logger.info(
            "[summary_recovery] process_incremental_1m done raw_rows=%d upsert_rows=%d persist=%s update_cache=%s",
            len(full_1m_raw),
            len(full_1m_upsert),
            persist,
            update_cache,
        )
        return full_1m_upsert

    except Exception:
        logger.exception("[summary_recovery] process_incremental_1m failed")
        return pd.DataFrame()


def process_incremental_higher_tf(
    df_1m: pd.DataFrame,
    interval: int,
    *,
    persist: bool = True,
    update_cache: bool = True,
) -> pd.DataFrame:
    try:
        if int(interval) not in (3, 5):
            raise ValueError(f"unsupported interval={interval}")

        out_raw = rebuild_higher_tf_from_1m(df_1m, int(interval))
        out_raw = normalize_datetime_columns(out_raw, interval=int(interval))

        if out_raw.empty:
            logger.info("[summary_recovery] process_incremental_higher_tf skipped: interval=%s empty", interval)
            return pd.DataFrame()

        out_raw = _apply_indicators_and_scoring(out_raw, interval=int(interval))
        out_raw = normalize_datetime_columns(out_raw, interval=int(interval))

        if out_raw.empty:
            logger.info(
                "[summary_recovery] process_incremental_higher_tf skipped after indicators/scoring: interval=%s empty",
                interval,
            )
            return pd.DataFrame()

        out_upsert = finalize_for_upsert(out_raw, int(interval))

        if persist and not out_upsert.empty:
            upsert_summary_df(out_upsert, int(interval))

        if update_cache and not out_raw.empty:
            update_global_cache(out_raw, int(interval))

        logger.info(
            "[summary_recovery] process_incremental_higher_tf done interval=%s raw_rows=%d upsert_rows=%d persist=%s update_cache=%s",
            interval,
            len(out_raw),
            len(out_upsert),
            persist,
            update_cache,
        )
        return out_upsert

    except Exception:
        logger.exception("[summary_recovery] process_incremental_higher_tf failed interval=%s", interval)
        return pd.DataFrame()