# ============================================================
# File   : trading/summary/ranking/dependencies.py
# Ver    : PRODUCTION-STABLE-RANKING-DEPENDENCIES-V1.0
#          -RANKING-ONLY
#          -NO-PUSH-REFERENCE
# ------------------------------------------------------------
# ✔ RANKING系依存解決専用
# ✔ PUSH系モジュールは一切参照しない
# ✔ RANKING runner / RANKING engine / display / fallback / cache を解決
# ✔ 新設 ranking 専用ファイルを最優先
# ✔ 旧互換 import は最小限だけ許容
# ============================================================

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ============================================================
# generic resolver
# ============================================================

def _resolve_callable(candidates: list[tuple[str, str]], label: str) -> Optional[Callable]:
    """
    候補を上から順に import して callable を返す。
    PUSH系は候補に入れない前提。
    """
    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info(
                    "[ranking.dependencies] resolved %s -> %s.%s",
                    label,
                    module_name,
                    func_name,
                )
                return fn

            logger.warning(
                "[ranking.dependencies] candidate attribute missing label=%s module=%s func=%s",
                label,
                module_name,
                func_name,
            )
        except Exception as e:
            logger.warning(
                "[ranking.dependencies] candidate import failed label=%s module=%s func=%s err=%s: %s",
                label,
                module_name,
                func_name,
                type(e).__name__,
                e,
            )
    return None


def _safe_signature_text(fn: Any) -> str:
    try:
        return str(inspect.signature(fn))
    except Exception:
        return "(signature unavailable)"


# ============================================================
# ranking summary runner resolver
# ============================================================

def resolve_ranking_summary_runner() -> Optional[Callable]:
    """
    RANKINGサマリー計算を実行する callable を返す。

    優先順位:
      1) 新設 ranking 専用 runner
      2) trading.ranking.ranking_summary_engine facade
      3) 旧 adapter / scheduler 互換

    PUSH系は一切候補に入れない。
    """
    candidates: list[tuple[str, str]] = [
        # 新設 ranking 専用 runner
        ("trading.summary.ranking.runner", "run_ranking_summary_job"),
        ("trading.summary.ranking.runner", "job_ranking_summary"),

        # ranking 本体 facade
        ("trading.ranking.ranking_summary_engine", "run_ranking_summary_job"),
        ("trading.ranking.ranking_summary_engine", "job_ranking_summary"),
        ("trading.ranking.ranking_summary_engine", "run_ranking_summary"),
        ("trading.ranking.ranking_summary_engine", "build_ranking_summary"),

        # 既存 adapter / compat
        ("trading.summary.engine.ranking_summary_engine_adapter", "run_ranking_summary_engine"),
        ("trading.summary.engine.ranking_summary_engine_adapter", "build_ranking_summary"),
        ("scheduler_jobs.summary.ranking_summary", "run_ranking_summary_job"),
        ("scheduler_jobs.summary.ranking_summary", "job_ranking_summary"),
    ]

    fn = _resolve_callable(candidates, label="ranking_summary_runner")
    if callable(fn):
        logger.info(
            "[ranking.dependencies] ranking_summary_runner signature=%s",
            _safe_signature_text(fn),
        )
    else:
        logger.warning("[ranking.dependencies] ranking_summary_runner unresolved")
    return fn


# ============================================================
# ranking display resolver
# ============================================================

def resolve_ranking_display() -> Optional[Callable]:
    """
    RANKING表示関数を返す。
    PUSH表示は候補に入れない。
    """
    candidates: list[tuple[str, str]] = [
        ("trading.summary.ranking.display", "display_ranking_summary"),
        ("scheduler_jobs.summary.display", "display_ranking_summary"),
        ("scheduler_jobs.summary.display_runner", "display_ranking_summary"),
    ]

    fn = _resolve_callable(candidates, label="ranking_display")
    if callable(fn):
        logger.info(
            "[ranking.dependencies] ranking_display signature=%s",
            _safe_signature_text(fn),
        )
    else:
        logger.warning("[ranking.dependencies] ranking_display unresolved")
    return fn


# ============================================================
# ranking fallback resolver
# ============================================================

def resolve_ranking_fallback_loader() -> Optional[Callable]:
    """
    RANKING fallback loader を返す。
    PUSH fallback は候補に入れない。
    """
    candidates: list[tuple[str, str]] = [
        ("trading.summary.ranking.fallback_loader", "fallback_ranking_summary_df"),
        ("scheduler_jobs.summary.fallback_loader", "fallback_ranking_summary_df"),
    ]

    fn = _resolve_callable(candidates, label="ranking_fallback_loader")
    if callable(fn):
        logger.info(
            "[ranking.dependencies] ranking_fallback_loader signature=%s",
            _safe_signature_text(fn),
        )
    else:
        logger.warning("[ranking.dependencies] ranking_fallback_loader unresolved")
    return fn


def resolve_ranking_row_filter() -> Optional[Callable]:
    """
    RANKING-like row filter を返す。
    push 混入除外用。
    """
    candidates: list[tuple[str, str]] = [
        ("trading.summary.ranking.fallback_loader", "filter_ranking_like_rows"),
        ("scheduler_jobs.summary.fallback_loader", "filter_ranking_like_rows"),
    ]

    fn = _resolve_callable(candidates, label="ranking_row_filter")
    if callable(fn):
        logger.info(
            "[ranking.dependencies] ranking_row_filter signature=%s",
            _safe_signature_text(fn),
        )
    else:
        logger.warning("[ranking.dependencies] ranking_row_filter unresolved")
    return fn


# ============================================================
# ranking cache writer resolver
# ============================================================

def resolve_ranking_cache_writer() -> Optional[Callable]:
    """
    RANKING保存関数を返す。
    """
    candidates: list[tuple[str, str]] = [
        ("trading.summary.ranking.cache_writer", "save_ranking_summary"),
        ("scheduler_jobs.summary.cache_writer", "save_ranking_summary"),
    ]

    fn = _resolve_callable(candidates, label="ranking_cache_writer")
    if callable(fn):
        logger.info(
            "[ranking.dependencies] ranking_cache_writer signature=%s",
            _safe_signature_text(fn),
        )
    else:
        logger.warning("[ranking.dependencies] ranking_cache_writer unresolved")
    return fn


# ============================================================
# optional guards resolver
# ============================================================

def resolve_ranking_quality_guard() -> Optional[Callable]:
    """
    RANKING未計算判定 guard を返す。
    """
    candidates: list[tuple[str, str]] = [
        ("trading.summary.ranking.guards", "looks_uncomputed_ranking_df"),
        ("scheduler_jobs.summary.quality_guards", "looks_uncomputed_ranking_df"),
    ]

    fn = _resolve_callable(candidates, label="ranking_quality_guard")
    if callable(fn):
        logger.info(
            "[ranking.dependencies] ranking_quality_guard signature=%s",
            _safe_signature_text(fn),
        )
    else:
        logger.warning("[ranking.dependencies] ranking_quality_guard unresolved")
    return fn


__all__ = [
    "resolve_ranking_summary_runner",
    "resolve_ranking_display",
    "resolve_ranking_fallback_loader",
    "resolve_ranking_row_filter",
    "resolve_ranking_cache_writer",
    "resolve_ranking_quality_guard",
]