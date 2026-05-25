# ============================================================
# File   : core/startup/global_context_summary_repair_patch.py
# Version: V1.0-MERGED-SUMMARY-TECH-REPAIR
# ------------------------------------------------------------
# 目的:
#   scheduler_jobs.summary.safe_io から GlobalContext.set_push_merged_summary()
#   へ入る直前のDFで、macd/signal/mtf が 0 に戻るケースを防ぐ。
#
# 背景:
#   push_summary_history_runtime_patch は push_summary_engine の戻り値を補正するが、
#   runner_core/safe_io 経由で再normalize/after_calcされたDFが
#   そのまま set_push_merged_summary されると、MERGED SET INPUT で
#   macd=0 signal=0 mtf=0 に退行することがある。
#
# 修正内容:
#   - global_context.set_merged_summary を monkey patch
#   - source=push の場合、set直前に summary_history_cache から同一銘柄の
#     最新非ゼロ macd/signal/slope/mtf を復元
#   - technical_ready も復元
# ============================================================

from __future__ import annotations

import logging
import os
from types import MethodType
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SET_MERGED = None

_FILL_COLS = (
    "macd", "signal", "hist", "rsi",
    "slope", "slope_atr_scaled", "score_slope",
    "mtf", "score_mtf", "mtf_score", "mtf_tf_count",
    "technical_ready", "symbol_hist_len",
)
_NONZERO_COLS = set(_FILL_COLS)


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    try:
        return pd.DataFrame(x).copy()
    except Exception:
        return pd.DataFrame()


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty or "symbol" not in out.columns:
        return pd.DataFrame()
    out = out.loc[:, ~out.columns.duplicated()].copy()
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


def _zero_like(v: Any) -> bool:
    try:
        if pd.isna(v):
            return True
    except Exception:
        pass
    try:
        return float(v) == 0.0
    except Exception:
        return str(v).strip() in {"", "0", "0.0", "False", "false", "None", "nan", "NaN"}


def _nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
    except Exception:
        return -1


def _best_map_from_history(hist: pd.DataFrame) -> dict[str, dict[str, Any]]:
    h = _normalize(hist)
    if h.empty or "symbol" not in h.columns:
        return {}
    if "datetime" in h.columns:
        h = h.sort_values(["symbol", "datetime"], kind="stable")
    result: dict[str, dict[str, Any]] = {}
    for sym, g in h.groupby("symbol", sort=False):
        sym_s = str(sym).strip()
        if not sym_s:
            continue
        d: dict[str, Any] = {}
        for col in _FILL_COLS:
            if col not in g.columns:
                continue
            val = None
            for v in reversed(list(g[col].values)):
                if not _zero_like(v):
                    val = v
                    break
            if val is None:
                last = g.iloc[-1].get(col)
                if not _zero_like(last):
                    val = last
            if val is not None:
                d[col] = val
        result[sym_s] = d
    return result


def _repair_df_from_history(gc, tf: Any, df: Any, source: str) -> pd.DataFrame:
    out = _normalize(df)
    if out.empty or source != "push":
        return _safe_df(df)

    try:
        hist = gc.get_summary_history(tf, source="push")
    except Exception:
        hist = pd.DataFrame()
    best = _best_map_from_history(hist)
    if not best:
        return out

    hits = fills = macd_fill = signal_fill = mtf_fill = 0
    for idx, row in out.iterrows():
        sym = str(row.get("symbol", "")).strip()
        vals = best.get(sym)
        if not vals:
            continue
        hits += 1
        for col, val in vals.items():
            if col not in out.columns:
                out[col] = pd.NA
            cur = out.at[idx, col]
            if _zero_like(cur) and not _zero_like(val):
                out.at[idx, col] = val
                fills += 1
                if col == "macd":
                    macd_fill += 1
                elif col == "signal":
                    signal_fill += 1
                elif col in {"mtf", "score_mtf", "mtf_score"}:
                    mtf_fill += 1

    try:
        if "technical_ready" not in out.columns:
            out["technical_ready"] = False
        ready = pd.Series(False, index=out.index)
        for col in ("macd", "signal", "rsi", "mtf", "score_mtf"):
            if col in out.columns:
                ready = ready | (pd.to_numeric(out[col], errors="coerce").fillna(0) != 0)
        out.loc[ready, "technical_ready"] = True
        if "symbol_hist_len" not in out.columns:
            out["symbol_hist_len"] = pd.NA
        out.loc[ready & out["symbol_hist_len"].isna(), "symbol_hist_len"] = 3
    except Exception:
        logger.exception("[GC SUMMARY REPAIR] technical_ready repair failed tf=%s", tf)

    logger.warning(
        "[GC SUMMARY REPAIR] tf=%s source=%s rows=%s hits=%s fills=%s macd_fill=%s signal_fill=%s mtf_fill=%s macd=%s signal=%s mtf=%s ready=%s",
        tf, source, len(out), hits, fills, macd_fill, signal_fill, mtf_fill,
        _nonzero_count(out, "macd"), _nonzero_count(out, "signal"), _nonzero_count(out, "mtf"), _nonzero_count(out, "technical_ready"),
    )
    return out


def install() -> bool:
    global _PATCHED, _ORIGINAL_SET_MERGED
    if _PATCHED:
        return True
    if not _env_bool("GLOBAL_CONTEXT_SUMMARY_REPAIR_ENABLED", True):
        logger.warning("[GC SUMMARY REPAIR] disabled by env")
        return False
    try:
        from core.global_context.context import global_context as GC

        orig = getattr(GC, "set_merged_summary", None)
        if not callable(orig):
            logger.warning("[GC SUMMARY REPAIR] set_merged_summary not callable")
            return False
        if getattr(orig, "_gc_summary_repair_patch", False):
            _PATCHED = True
            return True

        _ORIGINAL_SET_MERGED = orig

        def _patched_set_merged_summary(self, tf: Any, df: Any, source: str = "push") -> None:
            src = (source or "push").strip().lower()
            try:
                fixed = _repair_df_from_history(self, tf, df, src) if src == "push" else df
            except Exception:
                logger.exception("[GC SUMMARY REPAIR] repair failed tf=%s source=%s", tf, src)
                fixed = df
            return _ORIGINAL_SET_MERGED(tf=tf, df=fixed, source=source)

        _patched_set_merged_summary._gc_summary_repair_patch = True  # type: ignore[attr-defined]
        GC.set_merged_summary = MethodType(_patched_set_merged_summary, GC)
        _PATCHED = True
        logger.warning("[GC SUMMARY REPAIR] installed V1 set_merged_summary pre-store repair")
        return True
    except Exception:
        logger.exception("[GC SUMMARY REPAIR] install failed")
        return False


__all__ = ["install"]
