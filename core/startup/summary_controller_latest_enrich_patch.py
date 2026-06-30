# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_controller_latest_enrich_patch.py
# Version: V2-SUMMARY-CONTROLLER-LATEST-RANKING-ENRICH-SCORE-ALIASES
# ------------------------------------------------------------
# Purpose:
#   summary_controller の df_latest が保存/cache/entryへ流れる直前に
#   controller_enrich.enrich_summary_latest() を通す。
#
# V2:
#   - 2026-06-30 09:05ログで summary_recovery_push_1m の fetched/history-base が
#     score_buy/score_sell は nonzero なのに buy_score/sell_score は全0だった。
#   - buy_score/sell_score が空または全0の場合、score_buy/score_sell から復元する。
#   - display_score が全0の場合、final_score/score_total/score から復元する。
# ============================================================
from __future__ import annotations

import logging
from functools import wraps
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V2-SUMMARY-CONTROLLER-LATEST-RANKING-ENRICH-SCORE-ALIASES"
_INSTALLED = False


def _nonzero(df: Any, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return 0
        return int(pd.to_numeric(df[col], errors="coerce").fillna(0).ne(0).sum())
    except Exception:
        return 0


def _ensure_numeric_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        df[col] = 0.0
    return pd.to_numeric(df[col], errors="coerce").fillna(0.0)


def _repair_score_aliases(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        out = df.copy()
        changed: dict[str, tuple[int, int]] = {}

        for dst, src in (("buy_score", "score_buy"), ("sell_score", "score_sell")):
            if src in out.columns:
                before = _nonzero(out, dst)
                src_s = pd.to_numeric(out[src], errors="coerce").fillna(0.0)
                dst_s = _ensure_numeric_col(out, dst)
                if before == 0 and int(src_s.ne(0).sum()) > 0:
                    out[dst] = src_s
                else:
                    out[dst] = dst_s.where(dst_s.ne(0), src_s)
                after = _nonzero(out, dst)
                if after != before:
                    changed[dst] = (before, after)

        if "display_score" in out.columns:
            before = _nonzero(out, "display_score")
            disp = _ensure_numeric_col(out, "display_score")
            if before == 0:
                for src in ("final_score", "score_total", "score"):
                    if src in out.columns:
                        src_s = pd.to_numeric(out[src], errors="coerce").fillna(0.0)
                        if int(src_s.ne(0).sum()) > 0:
                            out["display_score"] = src_s
                            break
            else:
                for src in ("final_score", "score_total", "score"):
                    if src in out.columns:
                        src_s = pd.to_numeric(out[src], errors="coerce").fillna(0.0)
                        out["display_score"] = disp.where(disp.ne(0), src_s)
                        break
            after = _nonzero(out, "display_score")
            if after != before:
                changed["display_score"] = (before, after)

        if changed:
            logger.warning(
                "[SUMMARY CONTROLLER SCORE ALIAS REPAIR] context=%s interval=%s rows=%s changed=%s",
                context,
                interval,
                len(out),
                changed,
            )
        return out
    except Exception:
        logger.debug("[SUMMARY CONTROLLER SCORE ALIAS REPAIR] failed context=%s interval=%s", context, interval, exc_info=True)
        return df


def _enrich(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df
    try:
        from trading.summary.controller_enrich import enrich_summary_latest
        before_rank = _nonzero(df, "ranking_score")
        out = enrich_summary_latest(df, interval=int(interval or 1), context=context)
        out = _repair_score_aliases(out, interval=interval, context=context)
        after_rank = _nonzero(out, "ranking_score")
        if after_rank != before_rank or "ranking_score" not in df.columns:
            logger.warning(
                "[SUMMARY CONTROLLER LATEST ENRICH] context=%s interval=%s rows=%s ranking_score_nonzero %s->%s",
                context,
                interval,
                len(out) if isinstance(out, pd.DataFrame) else len(df),
                before_rank,
                after_rank,
            )
        return out if isinstance(out, pd.DataFrame) else df
    except Exception:
        logger.debug("[SUMMARY CONTROLLER LATEST ENRICH] enrich failed context=%s interval=%s", context, interval, exc_info=True)
        return _repair_score_aliases(df, interval=interval, context=context)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.summary.summary_controller as sc

        old_save = getattr(sc, "save_summary", None)
        if callable(old_save) and not getattr(old_save, "_latest_enrich_v2", False):
            @wraps(old_save)
            def _save_summary_enriched(df, interval, *args, **kwargs):
                return old_save(_enrich(df, interval=int(interval), context="before-save"), interval, *args, **kwargs)
            _save_summary_enriched._latest_enrich_v2 = True  # type: ignore[attr-defined]
            _save_summary_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _save_summary_enriched._original = old_save  # type: ignore[attr-defined]
            sc.save_summary = _save_summary_enriched

        old_run_ranking = getattr(sc, "run_ranking_pipeline", None)
        if callable(old_run_ranking) and not getattr(old_run_ranking, "_latest_enrich_v2", False):
            @wraps(old_run_ranking)
            def _run_ranking_enriched(df_latest, interval, *args, **kwargs):
                return old_run_ranking(_enrich(df_latest, interval=int(interval), context="before-ranking-pipeline"), interval, *args, **kwargs)
            _run_ranking_enriched._latest_enrich_v2 = True  # type: ignore[attr-defined]
            _run_ranking_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _run_ranking_enriched._original = old_run_ranking  # type: ignore[attr-defined]
            sc.run_ranking_pipeline = _run_ranking_enriched

        old_log_probe = getattr(sc, "log_scoring_probe", None)
        if callable(old_log_probe) and not getattr(old_log_probe, "_latest_enrich_v2", False):
            @wraps(old_log_probe)
            def _log_probe_enriched(label, interval, df, *args, **kwargs):
                return old_log_probe(label, interval, _enrich(df, interval=int(interval), context=f"log-{label}"), *args, **kwargs)
            _log_probe_enriched._latest_enrich_v2 = True  # type: ignore[attr-defined]
            _log_probe_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _log_probe_enriched._original = old_log_probe  # type: ignore[attr-defined]
            sc.log_scoring_probe = _log_probe_enriched

        old_set_latest = getattr(sc, "safe_global_set_latest", None)
        if callable(old_set_latest) and not getattr(old_set_latest, "_latest_enrich_v2", False):
            @wraps(old_set_latest)
            def _set_latest_enriched(interval, df, *args, **kwargs):
                return old_set_latest(interval, _enrich(df, interval=int(interval), context="cache-latest"), *args, **kwargs)
            _set_latest_enriched._latest_enrich_v2 = True  # type: ignore[attr-defined]
            _set_latest_enrich_v1 = True
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
