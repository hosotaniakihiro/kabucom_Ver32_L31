# ============================================================
# File   : core/startup/tonosama_datetime_tz_guard_patch.py
# Version: V1-TONOSAMA-DATETIME-TZ-NAIVE-GUARD
# ------------------------------------------------------------
# Purpose:
#   Prevent pandas sort/resample failures caused by mixed
#   tz-aware and tz-naive datetime values in TONOSAMA raw1 history.
#
#   The original tonosama_history_missing_guard_patch is kept intact.
#   This patch wraps its critical functions and normalizes datetime
#   values to JST tz-naive before drop_duplicates/sort/resample.
# ============================================================

from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_TO_DATETIME = pd.to_datetime


def _normalize_one_datetime(v: Any) -> Any:
    try:
        x = _ORIGINAL_TO_DATETIME(v, errors="coerce")
        if pd.isna(x):
            return pd.NaT
        tzinfo = getattr(x, "tzinfo", None)
        if tzinfo is not None:
            try:
                return x.tz_convert("Asia/Tokyo").tz_localize(None)
            except Exception:
                try:
                    return x.tz_localize(None)
                except Exception:
                    return pd.NaT
        return x
    except Exception:
        return pd.NaT


def _normalize_dt_jst_naive_series(s: Any) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            out = s.map(_normalize_one_datetime)
            return _ORIGINAL_TO_DATETIME(out, errors="coerce")
        out = pd.Series(s).map(_normalize_one_datetime)
        return _ORIGINAL_TO_DATETIME(out, errors="coerce")
    except Exception:
        try:
            return _ORIGINAL_TO_DATETIME(s, errors="coerce")
        except Exception:
            return pd.Series(pd.NaT, index=getattr(s, "index", None))


def _safe_to_datetime(arg: Any, *args: Any, **kwargs: Any) -> Any:
    """
    pandas.to_datetime compatible wrapper.

    Important:
    - tz-naive values are treated as already-JST wall-clock time and are not shifted.
    - tz-aware values are converted to Asia/Tokyo, then made tz-naive.
    - explicit utc=True calls are delegated to pandas unchanged.
    """
    try:
        if bool(kwargs.get("utc", False)):
            return _ORIGINAL_TO_DATETIME(arg, *args, **kwargs)
        if isinstance(arg, pd.Series):
            return _normalize_dt_jst_naive_series(arg)
        if isinstance(arg, pd.Index):
            return pd.DatetimeIndex(_normalize_dt_jst_naive_series(pd.Series(arg)))
        if isinstance(arg, (list, tuple)):
            return pd.DatetimeIndex(_normalize_dt_jst_naive_series(pd.Series(arg)))
        return _normalize_one_datetime(arg)
    except Exception:
        return _ORIGINAL_TO_DATETIME(arg, *args, **kwargs)


@contextlib.contextmanager
def _temporary_safe_to_datetime(pd_module: Any):
    old = getattr(pd_module, "to_datetime", None)
    try:
        pd_module.to_datetime = _safe_to_datetime
        yield
    finally:
        try:
            if old is not None:
                pd_module.to_datetime = old
        except Exception:
            pass


def _normalize_summary_datetime_df(df: Any) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty or "datetime" not in df.columns:
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    x = df.copy()
    try:
        x["datetime"] = _normalize_dt_jst_naive_series(x["datetime"])
        if "symbol" in x.columns:
            x["symbol"] = x["symbol"].astype(str).str.strip()
            x = x[x["symbol"] != ""].copy()
        x = x.dropna(subset=["datetime"])
        if "symbol" in x.columns and not x.empty:
            x = x.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
        elif not x.empty:
            x = x.sort_values(["datetime"], kind="stable").reset_index(drop=True)
    except Exception:
        logger.debug("[TONOSAMA DT TZ GUARD] dataframe datetime normalize failed", exc_info=True)
    return x


def _wrap_with_safe_datetime(module: Any, name: str) -> bool:
    old: Callable[..., Any] | None = getattr(module, name, None)
    if not callable(old) or getattr(old, "_tonosama_dt_tz_guard", False):
        return False

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            with _temporary_safe_to_datetime(module.pd):
                out = old(*args, **kwargs)
            return _normalize_summary_datetime_df(out)
        except Exception:
            logger.exception("[TONOSAMA DT TZ GUARD] wrapped function failed name=%s", name)
            return pd.DataFrame()

    _wrapped._tonosama_dt_tz_guard = True  # type: ignore[attr-defined]
    _wrapped._original = old  # type: ignore[attr-defined]
    setattr(module, name, _wrapped)
    return True


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import core.startup.tonosama_history_missing_guard_patch as target

        wrapped = []
        for name in ("_load_raw1_history", "_resample_1m_to_interval", "_compute_surge_features"):
            if _wrap_with_safe_datetime(target, name):
                wrapped.append(name)

        setattr(target, "_normalize_dt_jst_naive_series", _normalize_dt_jst_naive_series)
        setattr(target, "_TONOSAMA_DT_TZ_GUARD_PATCHED", True)
        _PATCHED = True
        logger.warning("[TONOSAMA DT TZ GUARD] installed wrapped=%s", wrapped)
        return True
    except Exception:
        logger.exception("[TONOSAMA DT TZ GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA DT TZ GUARD] auto install failed")


__all__ = ["install"]
