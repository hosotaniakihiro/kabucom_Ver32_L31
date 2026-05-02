# ============================================================
# File   : scheduler_jobs/summary/dependencies.py
# Function:
#   - 定時サマリー系の依存 callable を安全に解決する
#   - PUSH定時サマリー runner / RANKING定時サマリー runner を解決する
#   - PUSH / RANKING の display 関数を優先順位付きで解決する
#   - recursive / compat loop を避けながら、本線の callable を採用する
#   - 採用した callable の module / file / qualname / name を詳細ログ出力する
#   - compatibility resolver をまとめ、上位モジュールから再利用可能にする
# ------------------------------------------------------------
# Ver    : PRODUCTION-STABLE-SUMMARY-DEPENDENCIES-V7.0
#          -RUNNER-RESOLUTION-HARDENED
#          -DISPLAY-RESOLUTION-PRIORITY-FIX
#          -DISPLAY-RUNNER-BRIDGE-COMPAT
# ------------------------------------------------------------
# ✔ PUSH定時サマリー本線 resolver
# ✔ RANKING定時サマリー resolver
# ✔ display resolver の import 失敗理由を可視化
# ✔ scheduler_jobs.summary.display を最優先で解決
# ✔ scheduler_jobs.summary.display_runner 互換解決を追加
# ✔ recursive / compat loop を抑止
# ✔ 実際に採用した callable の module/file/qualname を詳細ログ
# ✔ backward compatibility resolver 群を維持
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ============================================================
# blocked recursive modules
# ============================================================

_BLOCKED_RUNNER_MODULES = {
    "scheduler_jobs.summary_jobs",
    "scheduler_jobs.summary.runners",
    "scheduler_jobs.summary.dependencies",
}

_BLOCKED_PREFIXES = (
    "scheduler_jobs.summary.dependencies",
)


# ============================================================
# basic helpers
# ============================================================

def _callable_desc(obj) -> str:
    try:
        return (
            f"module={getattr(obj, '__module__', None)} "
            f"qualname={getattr(obj, '__qualname__', None)} "
            f"name={getattr(obj, '__name__', None)}"
        )
    except Exception:
        return repr(obj)


def _module_file(module_name: str) -> Optional[str]:
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, "__file__", None)
    except Exception:
        return None


def _import_attr(module_name: str, attr_name: str, *, loud: bool = False):
    try:
        mod = importlib.import_module(module_name)
        obj = getattr(mod, attr_name, None)

        if obj is None:
            if loud:
                logger.error(
                    "[summary.dependencies] attr missing module=%s attr=%s module_file=%s",
                    module_name,
                    attr_name,
                    getattr(mod, "__file__", None),
                )
            else:
                logger.debug(
                    "[summary.dependencies] attr not found module=%s attr=%s module_file=%s",
                    module_name,
                    attr_name,
                    getattr(mod, "__file__", None),
                )
            return None

        if loud:
            logger.info(
                "[summary.dependencies] attr found module=%s attr=%s module_file=%s callable=%s desc=(%s)",
                module_name,
                attr_name,
                getattr(mod, "__file__", None),
                callable(obj),
                _callable_desc(obj),
            )

        return obj

    except Exception as e:
        if loud:
            logger.exception(
                "[summary.dependencies] import failed module=%s attr=%s err=%s",
                module_name,
                attr_name,
                e,
            )
        else:
            logger.debug(
                "[summary.dependencies] import failed module=%s attr=%s",
                module_name,
                attr_name,
                exc_info=True,
            )
        return None


def _is_blocked_runner(obj) -> bool:
    try:
        mod = getattr(obj, "__module__", "") or ""
        if mod in _BLOCKED_RUNNER_MODULES:
            return True
        return any(mod.startswith(prefix) for prefix in _BLOCKED_PREFIXES)
    except Exception:
        return False


def _log_resolved(label: str, obj) -> None:
    try:
        mod_name = getattr(obj, "__module__", None)
        logger.info(
            "[summary.dependencies] resolved %s -> %s file=%s desc=(%s)",
            label,
            mod_name,
            _module_file(mod_name) if mod_name else None,
            _callable_desc(obj),
        )
    except Exception:
        logger.exception("[summary.dependencies] resolved logging failed label=%s", label)


def _resolve_first_callable(
    candidates: list[tuple[str, str]],
    label: str,
) -> Optional[Callable]:
    logger.info(
        "[summary.dependencies] resolving %s candidates=%s",
        label,
        candidates,
    )

    for module_name, attr_name in candidates:
        loud = (
            module_name in (
                "scheduler_jobs.summary.display",
                "scheduler_jobs.summary.display_runner",
                "trading.summary.summary_controller",
            )
        )
        obj = _import_attr(module_name, attr_name, loud=loud)

        if not callable(obj):
            continue

        if _is_blocked_runner(obj):
            logger.warning(
                "[summary.dependencies] blocked recursive candidate %s -> %s.%s desc=(%s)",
                label,
                module_name,
                attr_name,
                _callable_desc(obj),
            )
            continue

        _log_resolved(label, obj)
        return obj

    logger.error(
        "[summary.dependencies] unresolved %s candidates=%s",
        label,
        candidates,
    )
    return None


# ============================================================
# push runner
# ============================================================

def _resolve_summary_controller_diff_update() -> Optional[Callable]:
    """
    summary_controller.diff_update の解決を試みる。
    返却関数の実体ログを強化して追跡しやすくする。
    """
    obj = _import_attr("trading.summary.summary_controller", "summary_controller", loud=True)
    if obj is not None:
        try:
            fn = getattr(obj, "diff_update", None)
            if callable(fn) and not _is_blocked_runner(fn):
                logger.info(
                    "[summary.dependencies] resolved controller diff_update -> trading.summary.summary_controller.summary_controller.diff_update desc=(%s)",
                    _callable_desc(fn),
                )
                return fn
        except Exception:
            logger.debug(
                "[summary.dependencies] diff_update resolve from summary_controller object failed",
                exc_info=True,
            )

    cls = _import_attr("trading.summary.summary_controller", "SummaryController", loud=True)
    if cls is not None:
        try:
            inst = cls()
            fn = getattr(inst, "diff_update", None)
            if callable(fn) and not _is_blocked_runner(fn):
                logger.info(
                    "[summary.dependencies] resolved controller diff_update -> trading.summary.summary_controller.SummaryController().diff_update desc=(%s)",
                    _callable_desc(fn),
                )
                return fn
        except Exception:
            logger.debug(
                "[summary.dependencies] SummaryController instance resolve failed",
                exc_info=True,
            )

    return None


def resolve_push_summary_runner() -> Optional[Callable]:
    """
    PUSH定時サマリーの実体 resolver。

    優先順位:
    1) summary_controller.diff_update
    2) trading.summary 系の実処理
    3) compat な job_summary / run_summary_job
    """
    fn = _resolve_summary_controller_diff_update()
    if callable(fn):
        _log_resolved("push_summary_runner", fn)
        return fn

    candidates: list[tuple[str, str]] = [
        ("trading.summary.engine.summary_incremental_engine", "run_summary_job"),
        ("trading.summary.engine.summary_incremental_engine", "job_summary"),
        ("trading.summary.engine.summary_engine", "run"),
        ("trading.summary.engine.summary_engine", "run_summary_engine"),
        ("trading.summary.engine.summary_engine", "summary_engine"),
        ("scheduler_jobs.summary.push_summary", "run_push_summary_job"),
        ("scheduler_jobs.summary.push_summary", "job_summary"),
    ]
    return _resolve_first_callable(candidates, "push_summary_runner")


# ============================================================
# ranking runner
# ============================================================

def resolve_ranking_summary_runner() -> Optional[Callable]:
    candidates: list[tuple[str, str]] = [
        ("trading.summary.engine.ranking_summary_engine", "job_ranking_summary"),
        ("trading.summary.engine.ranking_summary_engine", "run_ranking_summary_job"),
        ("trading.summary.engine.ranking_summary_engine", "run"),
        ("trading.summary.engine.ranking_summary_engine", "build_ranking_summary"),
        ("trading.ranking.ranking_summary_engine", "job_ranking_summary"),
        ("trading.ranking.ranking_summary_engine", "run_ranking_summary_job"),
        ("trading.ranking.ranking_summary_engine", "run"),
        ("scheduler_jobs.summary.ranking_summary", "run_ranking_summary_job"),
        ("scheduler_jobs.summary.ranking_summary", "job_ranking_summary"),
    ]
    return _resolve_first_callable(candidates, "ranking_summary_runner")


# ============================================================
# display resolver
# ============================================================

def resolve_display_push_summary() -> Optional[Callable]:
    """
    PUSH表示関数の resolver。
    scheduler_jobs.summary.display を最優先にしつつ、
    display_runner 互換も吸収する。
    """
    candidates: list[tuple[str, str]] = [
        ("scheduler_jobs.summary.display", "display_push_summary"),
        ("scheduler_jobs.summary.display", "display_summary"),
        ("scheduler_jobs.summary.display", "print_summary_top10"),
        ("scheduler_jobs.summary.display", "print_push_summary"),
        ("scheduler_jobs.summary.display_runner", "display_push_summary"),
        ("scheduler_jobs.summary.display_runner", "display_summary"),
        ("scheduler_jobs.summary_jobs", "display_summary_top10"),
        ("scheduler_jobs.summary_jobs", "print_summary_top10"),
        ("scheduler_jobs.summary_jobs", "display_push_summary"),
        ("scheduler_jobs.summary_jobs", "print_push_summary"),
    ]
    return _resolve_first_callable(candidates, "display_push_summary")


def resolve_display_ranking_summary() -> Optional[Callable]:
    """
    RANKING表示関数の resolver。
    scheduler_jobs.summary.display を最優先にしつつ、
    display_runner 互換も吸収する。
    """
    candidates: list[tuple[str, str]] = [
        ("scheduler_jobs.summary.display", "display_ranking_summary"),
        ("scheduler_jobs.summary.display", "print_ranking_summary_top10"),
        ("scheduler_jobs.summary.display", "print_ranking_summary"),
        ("scheduler_jobs.summary.display_runner", "display_ranking_summary"),
        ("scheduler_jobs.summary.display_runner", "display_summary"),
        ("scheduler_jobs.summary_jobs", "display_ranking_summary_top10"),
        ("scheduler_jobs.summary_jobs", "print_ranking_summary_top10"),
        ("scheduler_jobs.summary_jobs", "display_ranking_summary"),
        ("scheduler_jobs.summary_jobs", "print_ranking_summary"),
    ]
    return _resolve_first_callable(candidates, "display_ranking_summary")


def resolve_display_functions():
    push = resolve_display_push_summary()
    ranking = resolve_display_ranking_summary()

    logger.info(
        "[summary.dependencies] resolve_display_functions result push=(%s) ranking=(%s)",
        _callable_desc(push) if push else None,
        _callable_desc(ranking) if ranking else None,
    )

    return push, ranking


# ============================================================
# recovery / compatibility
# ============================================================

def resolve_bootstrap_incremental_rebuild() -> Optional[Callable]:
    candidates: list[tuple[str, str]] = [
        ("trading.summary.engine.summary_recovery_engine", "bootstrap_incremental_rebuild_from_push"),
        ("trading.summary.engine.summary_recovery_engine", "run"),
    ]
    return _resolve_first_callable(candidates, "bootstrap_incremental_rebuild")


def resolve_process_incremental_1m() -> Optional[Callable]:
    return resolve_push_summary_runner()


def resolve_process_incremental_3m() -> Optional[Callable]:
    return resolve_push_summary_runner()


def resolve_process_incremental_5m() -> Optional[Callable]:
    return resolve_push_summary_runner()


def resolve_process_incremental_higher_tf() -> Optional[Callable]:
    return resolve_push_summary_runner()


def resolve_process_ranking_summary() -> Optional[Callable]:
    return resolve_ranking_summary_runner()


def resolve_process_push_summary() -> Optional[Callable]:
    return resolve_push_summary_runner()


__all__ = [
    "resolve_push_summary_runner",
    "resolve_ranking_summary_runner",
    "resolve_display_push_summary",
    "resolve_display_ranking_summary",
    "resolve_display_functions",
    "resolve_bootstrap_incremental_rebuild",
    "resolve_process_incremental_1m",
    "resolve_process_incremental_3m",
    "resolve_process_incremental_5m",
    "resolve_process_incremental_higher_tf",
    "resolve_process_ranking_summary",
    "resolve_process_push_summary",
]