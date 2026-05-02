# ============================================================
# File   : core/startup/summary_runtime_pkg/post_bootstrap.py
# Version: REV3.0-SUMMARY-RUNTIME-POST-BOOTSTRAP
# ------------------------------------------------------------
# 【概要】
#   summary bootstrap 完了後 hook
#
# 【主な機能】
#   - push merged summary collect
#   - scoring_main 後追い実行
#   - entry pipeline 後追い実行
#   - summary 空時の DB seed retry
# ============================================================

from __future__ import annotations

import importlib
import logging

import pandas as pd

from core.startup.merged_summary_access import (
    get_push_merged_summary_safe,
    set_push_merged_summary_safe,
)

from . import state
from .state import SUMMARY_TFS
from .dataframe_utils import (
    normalize_datetime_for_tf,
    dedupe_symbol_datetime,
    log_summary_profile,
    symbols_count,
    latest_dt_str,
)
from .db_seed import seed_runtime_summary_cache_from_db

logger = logging.getLogger(__name__)


def get_scoring_main():
    try:
        from trading.scoring.core.scoring_core import scoring_main

        return scoring_main
    except Exception:
        logger.exception("[summary_runtime] scoring_main import failed")
        return None


def get_entry_pipeline():
    candidates = [
        ("trading.entry.run_entry_pipeline", "run_entry_pipeline"),
        ("trading.handlers.entry_controller", "run_entry_pipeline"),
    ]

    for module_name, attr_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, attr_name, None)
            if callable(fn):
                logger.info(
                    "[summary_runtime] entry pipeline resolved %s.%s",
                    module_name,
                    attr_name,
                )
                return fn
        except Exception:
            logger.debug(
                "[summary_runtime] entry pipeline candidate failed %s.%s",
                module_name,
                attr_name,
                exc_info=True,
            )

    logger.warning("[summary_runtime] entry pipeline unresolved")
    return None


def collect_push_merged_map() -> dict[int, pd.DataFrame]:
    out: dict[int, pd.DataFrame] = {}

    for tf in SUMMARY_TFS:
        try:
            df = get_push_merged_summary_safe(tf)
            if isinstance(df, pd.DataFrame):
                out[tf] = df.copy()
            else:
                out[tf] = pd.DataFrame()
        except Exception:
            logger.exception("[summary_runtime] collect push merged failed tf=%s", tf)
            out[tf] = pd.DataFrame()

        log_summary_profile("post-bootstrap-collect", tf, out[tf])

    return out


def has_any_summary(summary_map: dict[int, pd.DataFrame]) -> bool:
    return any(isinstance(df, pd.DataFrame) and not df.empty for df in summary_map.values())


def run_post_bootstrap_scoring(summary_map: dict[int, pd.DataFrame]) -> bool:
    scoring_main = get_scoring_main()
    if not callable(scoring_main):
        logger.warning("[summary_runtime] post-bootstrap scoring skipped reason=scoring_main_unavailable")
        return False

    ran_any = False

    for tf in SUMMARY_TFS:
        df = summary_map.get(tf, pd.DataFrame())

        if df is None or df.empty:
            logger.info("[summary_runtime] post-bootstrap scoring skip tf=%s reason=empty_summary", tf)
            continue

        try:
            log_summary_profile("post-bootstrap-scoring-input", tf, df)

            result = scoring_main(df, interval=tf)

            if isinstance(result, pd.DataFrame) and not result.empty:
                result = normalize_datetime_for_tf(result, tf)
                result = dedupe_symbol_datetime(result)
                log_summary_profile("post-bootstrap-scoring-output", tf, result)
                set_push_merged_summary_safe(tf, result)
                ran_any = True
                logger.info(
                    "[summary_runtime] post-bootstrap scoring stored output tf=%s rows=%d symbols=%d latest_dt=%s",
                    tf,
                    len(result),
                    symbols_count(result),
                    latest_dt_str(result),
                )
            else:
                set_push_merged_summary_safe(tf, df)
                ran_any = True
                logger.info(
                    "[summary_runtime] post-bootstrap scoring returned non-df tf=%s -> input preserved rows=%d",
                    tf,
                    len(df),
                )

        except Exception:
            logger.exception("[summary_runtime] post-bootstrap scoring failed tf=%s", tf)

    if ran_any:
        logger.info("[summary_runtime] post-bootstrap scoring done")
    else:
        logger.warning("[summary_runtime] post-bootstrap scoring did not run any tf")

    return ran_any


def run_post_bootstrap_entry(*, scoring_ran: bool, summary_ready: bool) -> bool:
    if not summary_ready and not scoring_ran:
        logger.warning(
            "[summary_runtime] post-bootstrap entry skipped reason=summary_not_ready"
        )
        return False

    fn = get_entry_pipeline()
    if not callable(fn):
        logger.warning(
            "[summary_runtime] post-bootstrap entry skipped reason=entry_pipeline_unavailable"
        )
        return False

    try:
        try:
            fn(source="summary_bootstrap")
        except TypeError:
            try:
                fn(pipeline_source="summary_bootstrap")
            except TypeError:
                fn()

        logger.info("[summary_runtime] post-bootstrap entry pipeline done")
        return True

    except Exception:
        logger.exception("[summary_runtime] post-bootstrap entry pipeline failed")
        return False


def run_post_bootstrap_hook(*, run_entry: bool = True) -> None:
    if state.POST_BOOTSTRAP_HOOK_RUNNING:
        logger.info("[summary_runtime] post-bootstrap hook already running -> skip")
        return

    if state.POST_BOOTSTRAP_HOOK_DONE:
        logger.info("[summary_runtime] post-bootstrap hook already done -> skip")
        return

    state.set_post_hook_flags(running=True, failed=False)

    try:
        logger.info("[summary_runtime] post-bootstrap hook start run_entry=%s", run_entry)

        summary_map = collect_push_merged_map()
        summary_ready = has_any_summary(summary_map)

        if not summary_ready:
            logger.warning(
                "[summary_runtime] post-bootstrap hook summary empty -> retry DB seed"
            )
            seed_result = seed_runtime_summary_cache_from_db(
                force=True,
                stage="post-bootstrap-empty-retry",
                rebuild_missing_scores=True,
            )
            logger.info("[summary_runtime] post-bootstrap DB seed retry result=%s", seed_result)

            summary_map = collect_push_merged_map()
            summary_ready = has_any_summary(summary_map)

        if not summary_ready:
            logger.warning(
                "[summary_runtime] post-bootstrap hook summary still empty -> scoring/entry skipped"
            )
            state.set_post_hook_flags(done=True)
            return

        scoring_ran = run_post_bootstrap_scoring(summary_map)

        refreshed = collect_push_merged_map()
        summary_ready_after = has_any_summary(refreshed)

        if run_entry:
            run_post_bootstrap_entry(
                scoring_ran=scoring_ran,
                summary_ready=summary_ready_after,
            )
        else:
            logger.info("[summary_runtime] post-bootstrap entry skipped run_entry=False")

        state.set_post_hook_flags(done=True)
        logger.info(
            "[summary_runtime] post-bootstrap hook done scoring_ran=%s summary_ready_before=%s summary_ready_after=%s run_entry=%s",
            scoring_ran,
            summary_ready,
            summary_ready_after,
            run_entry,
        )

    except Exception:
        state.set_post_hook_flags(failed=True)
        logger.exception("[summary_runtime] post-bootstrap hook failed")

    finally:
        state.set_post_hook_flags(running=False)


__all__ = [
    "get_scoring_main",
    "get_entry_pipeline",
    "collect_push_merged_map",
    "has_any_summary",
    "run_post_bootstrap_scoring",
    "run_post_bootstrap_entry",
    "run_post_bootstrap_hook",
]