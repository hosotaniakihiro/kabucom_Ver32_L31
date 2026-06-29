# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_controller_latest_enrich_patch.py
# Version: V1-SUMMARY-CONTROLLER-LATEST-RANKING-ENRICH
# ------------------------------------------------------------
# Purpose:
#   summary_controller の df_latest が保存/cache/entryへ流れる直前に
#   controller_enrich.enrich_summary_latest() を通す。
#
# Background:
#   2026-06-29 14:43ログで PUSH/summary/MTF は正常化したが、
#   1分足 projection では ranking_score=MISSING が残った。
#   controller_enrich.py には ranking_score 付与処理があるが、
#   summary_controller.py から呼ばれていないため。
# ============================================================
from __future__ import annotations

import logging
from functools import wraps
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-CONTROLLER-LATEST-RANKING-ENRICH"
_INSTALLED = False


def _score_nonzero(df: Any) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "ranking_score" not in df.columns:
            return 0
        return int(pd.to_numeric(df["ranking_score"], errors="coerce").fillna(0).ne(0).sum())
    except Exception:
        return 0


def _enrich(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df
    try:
        from trading.summary.controller_enrich import enrich_summary_latest
        before = _score_nonzero(df)
        out = enrich_summary_latest(df, interval=int(interval or 1), context=context)
        after = _score_nonzero(out)
        if after != before or "ranking_score" not in df.columns:
            logger.warning(
                "[SUMMARY CONTROLLER LATEST ENRICH] context=%s interval=%s rows=%s ranking_score_nonzero %s->%s",
                context,
                interval,
                len(out) if isinstance(out, pd.DataFrame) else len(df),
                before,
                after,
            )
        return out if isinstance(out, pd.DataFrame) else df
    except Exception:
        logger.debug("[SUMMARY CONTROLLER LATEST ENRICH] enrich failed context=%s interval=%s", context, interval, exc_info=True)
        return df


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.summary.summary_controller as sc

        old_save = getattr(sc, "save_summary", None)
        if callable(old_save) and not getattr(old_save, "_latest_enrich_v1", False):
            @wraps(old_save)
            def _save_summary_enriched(df, interval, *args, **kwargs):
                return old_save(_enrich(df, interval=int(interval), context="before-save"), interval, *args, **kwargs)
            _save_summary_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _save_summary_enriched._original = old_save  # type: ignore[attr-defined]
            sc.save_summary = _save_summary_enriched

        old_run_ranking = getattr(sc, "run_ranking_pipeline", None)
        if callable(old_run_ranking) and not getattr(old_run_ranking, "_latest_enrich_v1", False):
            @wraps(old_run_ranking)
            def _run_ranking_enriched(df_latest, interval, *args, **kwargs):
                return old_run_ranking(_enrich(df_latest, interval=int(interval), context="before-ranking-pipeline"), interval, *args, **kwargs)
            _run_ranking_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _run_ranking_enriched._original = old_run_ranking  # type: ignore[attr-defined]
            sc.run_ranking_pipeline = _run_ranking_enriched

        old_log_probe = getattr(sc, "log_scoring_probe", None)
        if callable(old_log_probe) and not getattr(old_log_probe, "_latest_enrich_v1", False):
            @wraps(old_log_probe)
            def _log_probe_enriched(label, interval, df, *args, **kwargs):
                return old_log_probe(label, interval, _enrich(df, interval=int(interval), context=f"log-{label}"), *args, **kwargs)
            _log_probe_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _log_probe_enriched._original = old_log_probe  # type: ignore[attr-defined]
            sc.log_scoring_probe = _log_probe_enriched

        old_set_latest = getattr(sc, "safe_global_set_latest", None)
        if callable(old_set_latest) and not getattr(old_set_latest, "_latest_enrich_v1", False):
            @wraps(old_set_latest)
            def _set_latest_enriched(interval, df, *args, **kwargs):
                return old_set_latest(interval, _enrich(df, interval=int(interval), context="cache-latest"), *args, **kwargs)
            _set_latest_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _set_latest_enriched._original = old_set_latest  # type: ignore[attr-defined]
            sc.safe_global_set_latest = _set_latest_enriched

        _INSTALLED = True
        logger.warning("[SUMMARY CONTROLLER LATEST ENRICH] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY CONTROLLER LATEST ENRICH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY CONTROLLER LATEST ENRICH] auto install failed")

__all__ = ["VERSION", "install"]
