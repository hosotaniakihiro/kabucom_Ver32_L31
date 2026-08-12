# -*- coding: utf-8 -*-
"""Prefer fresher PUSH summary over older rich merged cache."""
from __future__ import annotations

import datetime as dt
import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-FRESH-OVERWRITE"
_INSTALLED = False
_ORIGINAL = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return float(default)


def _latest_dt(df):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "end_time", "time", "snapshot_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                s = s.dropna()
                if not s.empty:
                    return pd.Timestamp(s.max())
    except Exception:
        pass
    return None


def _age_sec(ts) -> float:
    try:
        if ts is None:
            return 999999.0
        t = pd.Timestamp(ts)
        try:
            t = t.tz_localize(None)
        except Exception:
            pass
        return max(0.0, float((pd.Timestamp(dt.datetime.now()) - t).total_seconds()))
    except Exception:
        return 999999.0


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    try:
        import trading.summary.controller_cache as cc
        original = getattr(cc, "should_overwrite_merged_summary", None)
        if not callable(original):
            logger.warning("[SUMMARY FRESH OVERWRITE] original function missing")
            return False
        _ORIGINAL = original

        def patched_should_overwrite(existing_df, candidate_df):
            existing_latest = _latest_dt(existing_df)
            candidate_latest = _latest_dt(candidate_df)
            max_existing_age = _env_float("SUMMARY_FRESH_OVERWRITE_EXISTING_STALE_SEC", 180.0)
            min_newer_sec = _env_float("SUMMARY_FRESH_OVERWRITE_MIN_NEWER_SEC", 30.0)
            existing_age = _age_sec(existing_latest)
            try:
                if existing_latest is not None and candidate_latest is not None:
                    newer_sec = float((pd.Timestamp(candidate_latest) - pd.Timestamp(existing_latest)).total_seconds())
                    if existing_age >= max_existing_age and newer_sec >= min_newer_sec:
                        logger.warning(
                            "[SUMMARY FRESH OVERWRITE] force overwrite existing_latest=%s candidate_latest=%s existing_age=%.1fs newer_sec=%.1fs existing_rows=%s candidate_rows=%s",
                            existing_latest,
                            candidate_latest,
                            existing_age,
                            newer_sec,
                            len(existing_df) if isinstance(existing_df, pd.DataFrame) else -1,
                            len(candidate_df) if isinstance(candidate_df, pd.DataFrame) else -1,
                        )
                        return True
            except Exception:
                logger.debug("[SUMMARY FRESH OVERWRITE] freshness check failed", exc_info=True)
            return original(existing_df, candidate_df)

        cc.should_overwrite_merged_summary = patched_should_overwrite
        _INSTALLED = True
        logger.warning("[SUMMARY FRESH OVERWRITE] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY FRESH OVERWRITE] install failed")
        return False


__all__ = ["VERSION", "install"]
