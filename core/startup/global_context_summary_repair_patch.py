# ============================================================
# File   : core/startup/global_context_summary_repair_patch.py
# Version: V1.2-MERGED-SUMMARY-TECH-REPAIR-RECENT-PUSH-GET-SET
# ------------------------------------------------------------
# 目的:
#   scheduler_jobs.summary.safe_io から GlobalContext.set_push_merged_summary()
#   へ入る直前のDFで、macd/signal/mtf が 0 に戻るケースを防ぐ。
#
# V1.2:
#   - set_merged_summary だけでなく get_merged_summary もラップ
#   - source=push または source未指定fallbackで返る古い completed summary を除外
#   - 13:22ログのように MERGED GET FALLBACK tf=3/5 source=push rows=4084/4093 で
#     2026-05-26 / 2026-05-25 / 2026-05-19 の古い行が返る問題を防ぐ
#   - 古い行だけなら空DFを返し、殿様/5MA/SUMMARY AIが前日・先週足を参照しない
#
# V1.1:
#   - source=push の merged 保存前に、古い日付の行を除外
#   - 当日かつ直近N分のみを merged latest として扱う
#
# ENV:
#   GC_SUMMARY_REPAIR_FILTER_STALE_PUSH=1
#   GC_SUMMARY_REPAIR_PUSH_MAX_AGE_MIN=240
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from types import MethodType
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SET_MERGED = None
_ORIGINAL_GET_MERGED = None

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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return max(1.0, float(v))
    except Exception:
        return float(default)


def _now_naive() -> dt.datetime:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive().replace(tzinfo=None)
    except Exception:
        return dt.datetime.now()


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


def _filter_recent_push_rows(df: pd.DataFrame, *, tf: Any, source: str) -> pd.DataFrame:
    if df is None or df.empty or source != "push":
        return df
    if not _env_bool("GC_SUMMARY_REPAIR_FILTER_STALE_PUSH", True):
        return df
    if "datetime" not in df.columns:
        return df
    try:
        out = df.copy()
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        before = len(out)
        now = _now_naive()
        today = pd.Timestamp(now.date())
        max_age_min = _env_float("GC_SUMMARY_REPAIR_PUSH_MAX_AGE_MIN", 240.0)
        cutoff = pd.Timestamp(now - dt.timedelta(minutes=max_age_min))
        out = out[(out["datetime"].notna()) & (out["datetime"] >= today) & (out["datetime"] >= cutoff)].copy()
        dropped = before - len(out)
        if dropped > 0:
            try:
                min_dt = pd.to_datetime(df["datetime"], errors="coerce").min()
                max_dt = pd.to_datetime(df["datetime"], errors="coerce").max()
            except Exception:
                min_dt = max_dt = None
            logger.warning(
                "[GC SUMMARY REPAIR] stale push rows filtered tf=%s source=%s before=%s after=%s dropped=%s cutoff=%s today=%s original_min_dt=%s original_max_dt=%s",
                tf,
                source,
                before,
                len(out),
                dropped,
                cutoff,
                today.date(),
                min_dt,
                max_dt,
            )
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[GC SUMMARY REPAIR] stale push filter failed tf=%s source=%s", tf, source)
        return df


def _filter_get_result(df: pd.DataFrame, *, tf: Any, source: str | None) -> pd.DataFrame:
    """get_merged_summaryの戻り値側ガード。push/fallbackの古い行を返さない。"""
    out = _safe_df(df)
    if out.empty:
        return out
    src = (source or "").strip().lower()
    # source未指定時は fallback order が push優先なので、古いPUSH混入防止として同じガードを掛ける。
    if src in {"", "push", "push-cache", "push-legacy-attr", "completed"}:
        filtered = _filter_recent_push_rows(_normalize(out), tf=tf, source="push")
        if filtered.empty and not out.empty:
            logger.warning(
                "[GC SUMMARY REPAIR] get stale push fallback suppressed tf=%s source=%s before=%s after=0",
                tf,
                source,
                len(out),
            )
        return filtered
    return out


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
        return _filter_recent_push_rows(out, tf=tf, source=source)

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

    out = _filter_recent_push_rows(out, tf=tf, source=source)

    logger.warning(
        "[GC SUMMARY REPAIR] tf=%s source=%s rows=%s hits=%s fills=%s macd_fill=%s signal_fill=%s mtf_fill=%s macd=%s signal=%s mtf=%s ready=%s",
        tf, source, len(out), hits, fills, macd_fill, signal_fill, mtf_fill,
        _nonzero_count(out, "macd"), _nonzero_count(out, "signal"), _nonzero_count(out, "mtf"), _nonzero_count(out, "technical_ready"),
    )
    return out


def install() -> bool:
    global _PATCHED, _ORIGINAL_SET_MERGED, _ORIGINAL_GET_MERGED
    if _PATCHED:
        return True
    if not _env_bool("GLOBAL_CONTEXT_SUMMARY_REPAIR_ENABLED", True):
        logger.warning("[GC SUMMARY REPAIR] disabled by env")
        return False
    try:
        from core.global_context.context import global_context as GC

        orig_set = getattr(GC, "set_merged_summary", None)
        orig_get = getattr(GC, "get_merged_summary", None)
        if not callable(orig_set):
            logger.warning("[GC SUMMARY REPAIR] set_merged_summary not callable")
            return False
        if not callable(orig_get):
            logger.warning("[GC SUMMARY REPAIR] get_merged_summary not callable")
            return False
        if getattr(orig_set, "_gc_summary_repair_patch_v12", False):
            _PATCHED = True
            return True

        _ORIGINAL_SET_MERGED = orig_set
        _ORIGINAL_GET_MERGED = orig_get

        def _patched_set_merged_summary(self, tf: Any, df: Any, source: str = "push") -> None:
            src = (source or "push").strip().lower()
            try:
                fixed = _repair_df_from_history(self, tf, df, src) if src == "push" else df
            except Exception:
                logger.exception("[GC SUMMARY REPAIR] repair failed tf=%s source=%s", tf, src)
                fixed = df
            return _ORIGINAL_SET_MERGED(tf=tf, df=fixed, source=source)

        def _patched_get_merged_summary(self, tf: Any, source: str | None = None) -> pd.DataFrame:
            df = _ORIGINAL_GET_MERGED(tf=tf, source=source)
            try:
                return _filter_get_result(df, tf=tf, source=source)
            except Exception:
                logger.exception("[GC SUMMARY REPAIR] get filter failed tf=%s source=%s", tf, source)
                return df

        _patched_set_merged_summary._gc_summary_repair_patch_v12 = True  # type: ignore[attr-defined]
        _patched_get_merged_summary._gc_summary_repair_get_patch_v12 = True  # type: ignore[attr-defined]
        GC.set_merged_summary = MethodType(_patched_set_merged_summary, GC)
        GC.get_merged_summary = MethodType(_patched_get_merged_summary, GC)
        _PATCHED = True
        logger.warning("[GC SUMMARY REPAIR] installed V1.2 set/get merged_summary recent-push-only")
        return True
    except Exception:
        logger.exception("[GC SUMMARY REPAIR] install failed")
        return False


__all__ = ["install"]
