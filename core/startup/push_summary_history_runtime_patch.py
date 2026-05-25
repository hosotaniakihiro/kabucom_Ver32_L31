from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_RESOLVE = None
_ORIGINAL_STORE = None

_TECH_FILL_COLS = (
    "ma5", "ma25", "ma75", "rsi", "macd", "signal", "hist", "atr",
    "slope", "slope_atr_scaled", "score_slope", "mtf", "score_mtf", "mtf_score",
    "technical_ready", "symbol_hist_len",
)


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_df(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.DataFrame()


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty or "symbol" not in out.columns:
        return pd.DataFrame()
    out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = out[out["symbol"] != ""].copy()
    if "datetime" not in out.columns:
        for c in ("end_time", "start_time", "time"):
            if c in out.columns:
                out["datetime"] = out[c]
                break
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
    return out.reset_index(drop=True)


def _latest_dt(df: pd.DataFrame):
    try:
        if df.empty or "datetime" not in df.columns:
            return None
        s = pd.to_datetime(df["datetime"], errors="coerce")
        return s.max() if s.notna().any() else None
    except Exception:
        return None


def _nonzero(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
    except Exception:
        return -1


def _gc():
    try:
        from core.global_context.context import global_context as GC
        return GC
    except Exception:
        return None


def _get_history(interval: int) -> pd.DataFrame:
    GC = _gc()
    if GC is None:
        return pd.DataFrame()
    try:
        hist = GC.get_summary_history(interval, source="push")
    except TypeError:
        try:
            hist = GC.get_summary_history(interval)
        except Exception:
            hist = pd.DataFrame()
    except Exception:
        logger.exception("[PUSH HISTORY PATCH] get history failed interval=%s", interval)
        hist = pd.DataFrame()
    hist = _normalize(hist)
    if not hist.empty:
        logger.warning(
            "[PUSH HISTORY PATCH] history interval=%s rows=%s symbols=%s latest_dt=%s macd=%s signal=%s mtf=%s",
            interval, len(hist), hist["symbol"].nunique(), _latest_dt(hist),
            _nonzero(hist, "macd"), _nonzero(hist, "signal"), _nonzero(hist, "mtf"),
        )
    return hist


def _useful(hist: pd.DataFrame) -> bool:
    if hist.empty or "symbol" not in hist.columns:
        return False
    try:
        rows = len(hist)
        syms = int(hist["symbol"].nunique())
        if rows > max(10, syms * 2):
            return True
        if _nonzero(hist, "macd") > 0 or _nonzero(hist, "signal") > 0:
            return True
        if "symbol_hist_len" in hist.columns:
            return pd.to_numeric(hist["symbol_hist_len"], errors="coerce").max() >= 3
    except Exception:
        return False
    return False


def _patched_resolve(interval: int) -> pd.DataFrame:
    interval = int(interval)
    hist = _get_history(interval)
    if _useful(hist):
        logger.warning("[PUSH HISTORY PATCH] use history as pipeline seed interval=%s rows=%s", interval, len(hist))
        return hist
    if callable(_ORIGINAL_RESOLVE):
        return _ORIGINAL_RESOLVE(interval)
    return pd.DataFrame()


def _latest_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize(df)
    if out.empty:
        return out
    if "datetime" in out.columns:
        out = out.sort_values(["symbol", "datetime"], kind="stable")
    return out.drop_duplicates(subset=["symbol"], keep="last").reset_index(drop=True)


def _fill_from_history(df: pd.DataFrame, hist: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = _normalize(df)
    latest = _latest_by_symbol(hist)
    if out.empty or latest.empty:
        return out
    latest = latest.set_index("symbol", drop=False)
    fill_count = 0
    for idx, row in out.iterrows():
        sym = str(row.get("symbol", "")).strip()
        if not sym or sym not in latest.index:
            continue
        hrow = latest.loc[sym]
        if isinstance(hrow, pd.DataFrame):
            hrow = hrow.iloc[-1]
        for col in _TECH_FILL_COLS:
            if col not in hrow.index or pd.isna(hrow.get(col)):
                continue
            if col not in out.columns:
                out[col] = pd.NA
            cur = out.at[idx, col]
            need = pd.isna(cur)
            if not need:
                try:
                    need = float(cur) == 0.0 and float(hrow.get(col)) != 0.0
                except Exception:
                    need = str(cur).strip() in {"", "0", "0.0", "False"}
            if need:
                out.at[idx, col] = hrow.get(col)
                fill_count += 1
    logger.warning(
        "[PUSH HISTORY PATCH] filled interval=%s rows=%s fill_count=%s macd=%s signal=%s mtf=%s",
        interval, len(out), fill_count, _nonzero(out, "macd"), _nonzero(out, "signal"), _nonzero(out, "mtf"),
    )
    return out


def _merge_history(hist: pd.DataFrame, latest: pd.DataFrame, interval: int) -> pd.DataFrame:
    hist = _normalize(hist)
    latest = _normalize(latest)
    if hist.empty:
        return latest
    if latest.empty:
        return hist
    merged = pd.concat([hist, latest], ignore_index=True, sort=False)
    if "source" not in merged.columns:
        merged["source"] = "push"
    if "interval" not in merged.columns:
        merged["interval"] = int(interval)
    subset = ["symbol"]
    if "datetime" in merged.columns:
        subset.append("datetime")
    return merged.sort_values(subset, kind="stable").drop_duplicates(subset=subset, keep="last").reset_index(drop=True)


def _set_history(interval: int, df: pd.DataFrame) -> None:
    GC = _gc()
    if GC is None or df.empty:
        return
    try:
        GC.set_summary_history(interval, df.copy(), source="push_history_patch")
    except Exception:
        logger.exception("[PUSH HISTORY PATCH] set history failed interval=%s", interval)


def _patched_store(interval: int, df: pd.DataFrame) -> None:
    interval = int(interval)
    hist = _get_history(interval)
    fixed = _fill_from_history(df, hist, interval) if _useful(hist) else _safe_df(df)
    merged = _merge_history(hist, fixed, interval)
    if not merged.empty:
        _set_history(interval, merged)
    if callable(_ORIGINAL_STORE):
        return _ORIGINAL_STORE(interval, fixed)
    return None


def install() -> bool:
    global _PATCHED, _ORIGINAL_RESOLVE, _ORIGINAL_STORE
    if _PATCHED:
        return True
    if not _env_bool("PUSH_SUMMARY_HISTORY_PATCH_ENABLED", True):
        return False
    try:
        import trading.summary.engine.push_summary_engine as pse
        old_resolve = getattr(pse, "_resolve_summary_source_df", None)
        if callable(old_resolve) and not getattr(old_resolve, "_push_history_patch", False):
            _ORIGINAL_RESOLVE = old_resolve
            _patched_resolve._push_history_patch = True  # type: ignore[attr-defined]
            pse._resolve_summary_source_df = _patched_resolve
            logger.warning("[PUSH HISTORY PATCH] patched resolve")
        old_store = getattr(pse, "_store_push_merged_summary", None)
        if callable(old_store) and not getattr(old_store, "_push_history_patch", False):
            _ORIGINAL_STORE = old_store
            _patched_store._push_history_patch = True  # type: ignore[attr-defined]
            pse._store_push_merged_summary = _patched_store
            logger.warning("[PUSH HISTORY PATCH] patched store")
        _PATCHED = True
        logger.warning("[PUSH HISTORY PATCH] installed V1")
        return True
    except Exception:
        logger.exception("[PUSH HISTORY PATCH] install failed")
        return False


__all__ = ["install"]
