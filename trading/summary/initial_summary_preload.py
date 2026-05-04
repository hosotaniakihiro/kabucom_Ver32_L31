# ============================================================
# File   : trading/summary/initial_summary_preload.py
# Ver    : PRODUCTION-STABLE-REV3-INITIAL-SUMMARY-PRELOAD
#          -COPYSAFE-LOADER-PRIMARY
# ------------------------------------------------------------
# ✔ REV2 全機能保持
# ✔ 起動直後（PUSHなし）でも最新サマリーを表示
# ✔ persistence.summary_loader を正とする
# ✔ latest_summary / merged_summary / multi_summary を同時更新
# ✔ 何も無くても落ちない
# ✔ summary_bootstrap と整合
# ✔ NEW: deep copy で global_data 汚染防止
# ✔ NEW: loader を正として preload 層では余計な加工をしない
# ✔ NEW: interval別ログ強化
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from global_state import global_data

from trading.summary.persistence.summary_loader import (
    load_recent_1min,
    load_recent_3min,
    load_recent_5min,
)

logger = logging.getLogger(__name__)


def _safe_copy_df(df: pd.DataFrame | None) -> pd.DataFrame:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        return df.copy(deep=True)
    except Exception:
        logger.exception("failed deep copy dataframe")
        return pd.DataFrame()


def _safe_set_attr(obj: Any, name: str, value: Any) -> None:
    try:
        setattr(obj, name, value)
    except Exception:
        logger.exception("failed setattr name=%s", name)


def _safe_set_latest_by_interval(interval: int, df: pd.DataFrame) -> None:
    try:
        target = getattr(global_data, "latest_summary_by_interval", None)
        if target is None:
            target = {}
            setattr(global_data, "latest_summary_by_interval", target)
        target[int(interval)] = _safe_copy_df(df)
    except Exception:
        logger.exception("failed latest_summary_by_interval interval=%s", interval)


def _safe_set_merged_summary(interval: int, df: pd.DataFrame) -> None:
    try:
        safe_df = _safe_copy_df(df)

        if hasattr(global_data, "set_merged_summary"):
            global_data.set_merged_summary(int(interval), safe_df)
            return

        merged = getattr(global_data, "merged_summary", None)
        if merged is None:
            merged = {}
            setattr(global_data, "merged_summary", merged)
        merged[int(interval)] = safe_df
    except Exception:
        logger.exception("failed merged_summary interval=%s", interval)


def _safe_set_multi_summary(interval: int, df: pd.DataFrame) -> None:
    try:
        safe_df = _safe_copy_df(df)

        if hasattr(global_data, "set_multi_summary"):
            global_data.set_multi_summary(int(interval), safe_df)
            return

        multi = getattr(global_data, "multi_summary", None)
        if multi is None:
            multi = {}
            setattr(global_data, "multi_summary", multi)
        multi[int(interval)] = safe_df
    except Exception:
        logger.exception("failed multi_summary interval=%s", interval)


def _log_preload_profile(interval: int, df: pd.DataFrame) -> None:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            logger.info("[INITIAL PRELOAD][%smin] empty", interval)
            return

        symbols = int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0

        latest_dt = None
        if "datetime" in df.columns:
            try:
                latest_dt = pd.to_datetime(df["datetime"], errors="coerce").max()
            except Exception:
                latest_dt = None

        logger.info(
            "[INITIAL PRELOAD][%smin] rows=%d symbols=%d latest_dt=%s has_score=%s has_slope=%s has_mtf=%s",
            interval,
            len(df),
            symbols,
            latest_dt,
            "score" in df.columns,
            "slope" in df.columns,
            "mtf" in df.columns,
        )
    except Exception:
        logger.exception("failed preload profile interval=%s", interval)


def _set_initial_summary(interval: int, df: pd.DataFrame) -> None:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return

    safe_df = _safe_copy_df(df)
    if safe_df.empty:
        return

    _safe_set_latest_by_interval(interval, safe_df)

    if int(interval) == 1:
        _safe_set_attr(global_data, "latest_summary_1m", _safe_copy_df(safe_df))
    elif int(interval) == 3:
        _safe_set_attr(global_data, "latest_summary_3m", _safe_copy_df(safe_df))
    elif int(interval) == 5:
        _safe_set_attr(global_data, "latest_summary_5m", _safe_copy_df(safe_df))

    _safe_set_merged_summary(interval, safe_df)
    _safe_set_multi_summary(interval, safe_df)


def preload_initial_summary() -> None:
    """
    起動直後（PUSH未到着）でも
    - 表示
    - スコア参照
    - ENTRY判定の前段参照
    が出来るように、DBから最新サマリーをロードする

    方針:
    - summary_loader を正とする
    - ここでは余計な加工を行わず、loader が返した安全な DataFrame を格納する
    """
    logger.info("📥 preload_initial_summary: start")

    rows_info = {"1min": 0, "3min": 0, "5min": 0}

    try:
        frames = {
            1: load_recent_1min(),
            3: load_recent_3min(),
            5: load_recent_5min(),
        }

        for interval, df in frames.items():
            try:
                if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                    logger.warning("⚠ preload %smin empty", interval)
                    continue

                safe_df = _safe_copy_df(df)
                if safe_df.empty:
                    logger.warning("⚠ preload %smin empty after copy", interval)
                    continue

                _set_initial_summary(interval, safe_df)
                rows_info[f"{interval}min"] = len(safe_df)

                _log_preload_profile(interval, safe_df)
                logger.info("✅ preload %smin rows=%d", interval, len(safe_df))

            except Exception:
                logger.exception("❌ preload %smin failed", interval)

        logger.info("📊 initial summary ready %s", rows_info)

    except Exception:
        logger.exception("❌ preload_initial_summary failed")

    logger.info("📥 preload_initial_summary: done")