# ============================================================
# File   : core/startup/summary_controller_enrich_runtime_patch.py
# Version: Ver01-SUMMARY-CONTROLLER-ENRICH-RUNTIME-PATCH
# ------------------------------------------------------------
# Purpose:
#   - システム稼働中の既存フローを壊さず、summary_controller が
#     表示/保存/ranking/AI/entry へ渡す df_latest を補強する。
#   - ranking_score / ranking_type / rank / change_rate / turnover を付与
#   - daily MTF / mtf_alignment を表示・保存前にも付与
#
# Why runtime patch:
#   - summary_controller.py 本体は大きく、場中の安全性優先で
#     monkey patch により最小差分で導入する。
# ============================================================

from __future__ import annotations

import logging
from functools import wraps
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINALS: dict[str, Any] = {}


def _detect_interval(args: tuple[Any, ...], kwargs: dict[str, Any], default: int = 1) -> int:
    try:
        if "interval" in kwargs:
            return int(kwargs.get("interval") or default)
        # common signatures:
        #   _safe_run_display(interval, df)
        #   save_summary(df, interval, ...)
        #   run_ranking_pipeline(df, interval)
        for v in args:
            if isinstance(v, int):
                return int(v)
            if isinstance(v, str) and v.isdigit():
                return int(v)
    except Exception:
        pass
    return int(default)


def _enrich_df(df: Any, *, interval: int, context: str) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        from trading.summary.controller_enrich import enrich_summary_latest

        out = enrich_summary_latest(df, interval=interval, context=context)
        logger.info(
            "[SUMMARY CONTROLLER ENRICH PATCH] context=%s interval=%s rows=%s cols=%s",
            context,
            interval,
            len(out) if isinstance(out, pd.DataFrame) else None,
            len(out.columns) if isinstance(out, pd.DataFrame) else None,
        )
        return out
    except Exception:
        logger.exception(
            "[SUMMARY CONTROLLER ENRICH PATCH] enrich failed context=%s interval=%s",
            context,
            interval,
        )
        return df


def _patch_safe_run_display(sc_mod: Any) -> bool:
    fn = getattr(sc_mod, "_safe_run_display", None)
    if not callable(fn) or "_safe_run_display" in _ORIGINALS:
        return False

    _ORIGINALS["_safe_run_display"] = fn

    @wraps(fn)
    def wrapped(interval: int, df_latest: Any, *args: Any, **kwargs: Any):
        df_latest = _enrich_df(df_latest, interval=int(interval or 1), context="before-display")
        return fn(interval, df_latest, *args, **kwargs)

    setattr(sc_mod, "_safe_run_display", wrapped)
    return True


def _patch_save_summary(sc_mod: Any) -> bool:
    fn = getattr(sc_mod, "save_summary", None)
    if not callable(fn) or "save_summary" in _ORIGINALS:
        return False

    _ORIGINALS["save_summary"] = fn

    @wraps(fn)
    def wrapped(df: Any, interval: int = 1, *args: Any, **kwargs: Any):
        df = _enrich_df(df, interval=int(interval or 1), context="before-save")
        return fn(df, interval, *args, **kwargs)

    setattr(sc_mod, "save_summary", wrapped)
    return True


def _patch_df_first_func(sc_mod: Any, name: str, context: str) -> bool:
    fn = getattr(sc_mod, name, None)
    if not callable(fn) or name in _ORIGINALS:
        return False

    _ORIGINALS[name] = fn

    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any):
        interval = _detect_interval(args, kwargs, default=1)
        if args and isinstance(args[0], pd.DataFrame):
            args = (_enrich_df(args[0], interval=interval, context=context),) + tuple(args[1:])
        elif isinstance(kwargs.get("df_latest"), pd.DataFrame):
            kwargs = dict(kwargs)
            kwargs["df_latest"] = _enrich_df(kwargs["df_latest"], interval=interval, context=context)
        elif isinstance(kwargs.get("df"), pd.DataFrame):
            kwargs = dict(kwargs)
            kwargs["df"] = _enrich_df(kwargs["df"], interval=interval, context=context)
        return fn(*args, **kwargs)

    setattr(sc_mod, name, wrapped)
    return True


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        logger.warning("[SUMMARY CONTROLLER ENRICH PATCH] already installed")
        return True

    try:
        import trading.summary.summary_controller as sc_mod

        patched = []
        if _patch_safe_run_display(sc_mod):
            patched.append("_safe_run_display")
        if _patch_save_summary(sc_mod):
            patched.append("save_summary")
        if _patch_df_first_func(sc_mod, "run_ranking_pipeline", "before-ranking-pipeline"):
            patched.append("run_ranking_pipeline")
        if _patch_df_first_func(sc_mod, "run_ai_pipeline", "before-ai-pipeline"):
            patched.append("run_ai_pipeline")
        if _patch_df_first_func(sc_mod, "run_entry_pipeline", "before-entry-pipeline"):
            patched.append("run_entry_pipeline")

        _INSTALLED = True
        logger.warning(
            "[SUMMARY CONTROLLER ENRICH PATCH] installed patched=%s",
            patched,
        )
        return True

    except Exception:
        logger.exception("[SUMMARY CONTROLLER ENRICH PATCH] install failed")
        return False


__all__ = ["install"]
