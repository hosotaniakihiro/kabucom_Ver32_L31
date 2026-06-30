# ============================================================
# File   : core/startup/tonosama_datetime_tz_guard_patch.py
# Version: V2-TONOSAMA-RAW1-HISTORY-TZ-NAIVE-GUARD
# ------------------------------------------------------------
# Purpose:
#   Prevent pandas sort/drop_duplicates/resample failures caused by mixed
#   tz-aware and tz-naive datetime values in TONOSAMA raw1 history.
#
#   V2:
#     - Replace tonosama_history_missing_guard_patch._load_raw1_history with
#       a safe implementation that normalizes every source to JST tz-naive
#       before concat/drop_duplicates/sort_values.
#     - Keep wrappers around resample/feature compute as a second line of
#       defense.
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


def _safe_df(obj: Any) -> pd.DataFrame:
    return obj if isinstance(obj, pd.DataFrame) else pd.DataFrame()


def _build_safe_load_raw1_history(target: Any):
    def _safe_load_raw1_history(vs: Any, *, interval: int = 1) -> pd.DataFrame:
        frames: list[tuple[str, pd.DataFrame]] = []
        try:
            if target._env_bool("TONOSAMA_RAW1_HISTORY_RESAMPLE", True):
                frames.append(("push_raw_db_history", _safe_df(target._load_push_raw_db_1m_history())))
                frames.append(("global_context_history", _safe_df(target._call_gc_history(1))))
                frames.append(("global_data_history", _safe_df(target._call_global_data_history(1))))
            try:
                frames.append(("merged_latest", _safe_df(vs.load_merged_summary(1))))
            except Exception:
                logger.debug("[TONOSAMA DT TZ GUARD] merged latest load failed", exc_info=True)

            norm_parts: list[pd.DataFrame] = []
            stats: list[dict[str, Any]] = []
            for label, df in frames:
                try:
                    if df is None or df.empty:
                        stats.append({"source": label, "rows": 0, "symbols": 0, "latest": None})
                        continue
                    with _temporary_safe_to_datetime(target.pd):
                        x = vs.normalize_summary_base(df, interval=1)
                    if x is None or x.empty or "datetime" not in x.columns:
                        stats.append({"source": label, "rows": len(df), "symbols": 0, "latest": None, "normalized": 0})
                        continue
                    x = _normalize_summary_datetime_df(x)
                    if x.empty or "symbol" not in x.columns or "datetime" not in x.columns:
                        continue
                    x = x.dropna(subset=["symbol", "datetime"])
                    if x.empty:
                        continue
                    x["_raw1_source"] = label
                    norm_parts.append(x)
                    stats.append({
                        "source": label,
                        "rows": len(x),
                        "symbols": int(x["symbol"].nunique()) if "symbol" in x.columns else 0,
                        "latest": str(x["datetime"].max()),
                    })
                except Exception:
                    logger.debug("[TONOSAMA DT TZ GUARD] normalize failed source=%s", label, exc_info=True)

            if not norm_parts:
                logger.warning("[TONOSAMA RAW1 HISTORY] no usable 1m history interval=%s stats=%s", interval, stats)
                return pd.DataFrame()

            out = pd.concat(norm_parts, ignore_index=True, sort=False)
            out = _normalize_summary_datetime_df(out)
            if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
                return pd.DataFrame()
            out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
            out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

            min_bars = target._raw1_min_bars_for_interval(interval)
            counts = out.groupby("symbol")["datetime"].transform("count") if not out.empty else pd.Series(dtype="int64")
            enough = out[counts >= min_bars].copy()
            if enough.empty:
                logger.warning(
                    "[TONOSAMA RAW1 HISTORY] insufficient 1m history interval=%s rows=%s symbols=%s min_bars=%s stats=%s tz_guard=v2",
                    interval,
                    len(out),
                    int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
                    min_bars,
                    stats,
                )
                return out
            logger.warning(
                "[TONOSAMA RAW1 HISTORY] loaded interval=%s rows=%s symbols=%s usable_rows=%s usable_symbols=%s min_bars=%s latest=%s stats=%s tz_guard=v2",
                interval,
                len(out),
                int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
                len(enough),
                int(enough["symbol"].nunique()) if "symbol" in enough.columns else 0,
                min_bars,
                enough["datetime"].max(),
                stats,
            )
            return enough.reset_index(drop=True)
        except Exception:
            logger.exception("[TONOSAMA DT TZ GUARD] safe _load_raw1_history failed interval=%s", interval)
            return pd.DataFrame()

    _safe_load_raw1_history._tonosama_dt_tz_guard_v2 = True  # type: ignore[attr-defined]
    return _safe_load_raw1_history


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

        # Stronger than the generic wrapper: avoid the original function's
        # concat/drop_duplicates/sort path entirely because that is where mixed
        # timezone values were failing in pandas.
        old_load = getattr(target, "_load_raw1_history", None)
        if callable(old_load) and not getattr(old_load, "_tonosama_dt_tz_guard_v2", False):
            safe_load = _build_safe_load_raw1_history(target)
            safe_load._original = old_load  # type: ignore[attr-defined]
            target._load_raw1_history = safe_load
            wrapped.append("_load_raw1_history:v2")

        for name in ("_resample_1m_to_interval", "_compute_surge_features"):
            if _wrap_with_safe_datetime(target, name):
                wrapped.append(name)

        setattr(target, "_normalize_dt_jst_naive_series", _normalize_dt_jst_naive_series)
        setattr(target, "_TONOSAMA_DT_TZ_GUARD_PATCHED", True)
        _PATCHED = True
        logger.warning("[TONOSAMA DT TZ GUARD] installed version=V2 wrapped=%s", wrapped)
        return True
    except Exception:
        logger.exception("[TONOSAMA DT TZ GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA DT TZ GUARD] auto install failed")


__all__ = ["install"]
