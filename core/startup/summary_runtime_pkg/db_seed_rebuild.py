# ============================================================
# File   : core/startup/summary_runtime_pkg/db_seed_rebuild.py
# Version: REV1.0-SUMMARY-RUNTIME-DB-SEED-REBUILD
# ------------------------------------------------------------
# 【概要】
#   起動時 summary DB seed 後の indicator / MTF / scoring rebuild
#
# 【主な機能】
#   ✔ score 不足時の fallback rebuild
#   ✔ MTF 全滅時の post-seed rebuild
#   ✔ optional htf pipeline fallback
# ============================================================

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Callable, Optional

import pandas as pd

from trading.summary.summary_post_processor import post_process_summary
from trading.summary.engine.processors.mtf import safe_mtf
from trading.summary.engine.processors.scoring import safe_scoring
from trading.summary.engine.guards.enhance_guard import enhance_guard
from trading.summary.engine.internal.scoring_guard import finalize_scoring

from .dataframe_utils import summary_has_ready_scores
from .db_seed_diagnostics import log_indicator_profile, safe_symbols_count
from .db_seed_policy import as_df, nonzero_count

logger = logging.getLogger(__name__)


def call_with_supported_kwargs(fn, **kwargs):
    try:
        sig = inspect.signature(fn)
        supported = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return fn(**supported)
    except TypeError:
        raise
    except Exception:
        raise


def maybe_rebuild_seed_indicators(df: pd.DataFrame, tf: int) -> pd.DataFrame:
    """
    score が不足している場合の fallback rebuild。
    """
    df = as_df(df)
    if df.empty:
        return df

    try:
        if summary_has_ready_scores(df):
            return df

        interval_name = f"{int(tf)}min"

        logger.info(
            "[summary_runtime] DB seed lacks ready scores -> fallback rebuild tf=%s rows=%d symbols=%d",
            tf,
            len(df),
            safe_symbols_count(df),
        )

        x = enhance_guard(df)
        x = safe_mtf(x)
        x = safe_scoring(x, interval_name)
        x = finalize_scoring(enhance_guard(x))
        x = post_process_summary(x)

        if isinstance(x, pd.DataFrame) and not x.empty:
            log_indicator_profile(
                x,
                tf=int(tf),
                label="DB seed fallback indicators rebuilt",
            )
            return x

        return df

    except Exception:
        logger.debug(
            "[summary_runtime] DB seed indicator fallback failed tf=%s",
            tf,
            exc_info=True,
        )
        return df


def need_post_seed_mtf_rebuild(df: pd.DataFrame) -> bool:
    """
    MTF 系が全滅しているか判定する。
    """
    df = as_df(df)
    if df.empty:
        return False

    mtf_cols = [c for c in ("mtf", "score_mtf", "mtf_score") if c in df.columns]
    if not mtf_cols:
        return True

    for c in mtf_cols:
        if nonzero_count(df, c) > 0:
            return False

    return True


def resolve_optional_callable(candidates: list[tuple[str, str]]) -> Optional[Callable]:
    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info(
                    "[summary_runtime] resolved optional callable %s.%s",
                    module_name,
                    func_name,
                )
                return fn
        except Exception:
            logger.debug(
                "[summary_runtime] optional callable import failed %s.%s",
                module_name,
                func_name,
                exc_info=True,
            )
    return None


def post_seed_mtf_rebuild(df: pd.DataFrame, tf: int) -> pd.DataFrame:
    """
    seed 後に MTF / score_mtf が全滅している場合の補修。
    """
    df = as_df(df)
    if df.empty:
        return df

    if not need_post_seed_mtf_rebuild(df):
        return df

    interval_name = f"{int(tf)}min"

    logger.info(
        "[summary_runtime] post-seed MTF rebuild needed tf=%s rows=%d symbols=%d "
        "mtf=%d score_mtf=%d mtf_score=%d",
        tf,
        len(df),
        safe_symbols_count(df),
        nonzero_count(df, "mtf"),
        nonzero_count(df, "score_mtf"),
        nonzero_count(df, "mtf_score"),
    )

    try:
        x = enhance_guard(df)
        x = safe_mtf(x)
        x = safe_scoring(x, interval_name)
        x = finalize_scoring(enhance_guard(x))
        x = post_process_summary(x)

        if isinstance(x, pd.DataFrame) and not x.empty:
            logger.info(
                "[summary_runtime] post-seed MTF rebuild done tf=%s rows=%d symbols=%d "
                "after_mtf=%d after_score_mtf=%d after_mtf_score=%d",
                tf,
                len(x),
                safe_symbols_count(x),
                nonzero_count(x, "mtf"),
                nonzero_count(x, "score_mtf"),
                nonzero_count(x, "mtf_score"),
            )
            return x

    except Exception:
        logger.debug(
            "[summary_runtime] post-seed MTF rebuild via safe_mtf failed tf=%s",
            tf,
            exc_info=True,
        )

    try:
        fn = resolve_optional_callable(
            [
                ("trading.summary.engine.htf_indicator_pipeline", "attach_mtf_indicators"),
                ("trading.summary.engine.htf_indicator_pipeline", "apply_mtf_indicators"),
                ("trading.summary.engine.htf_indicator_pipeline", "run_htf_indicator_pipeline"),
            ]
        )

        if callable(fn):
            x = call_with_supported_kwargs(
                fn,
                df=df,
                interval=interval_name,
                tf=int(tf),
            )
            if isinstance(x, pd.DataFrame) and not x.empty:
                x = enhance_guard(x)
                x = safe_scoring(x, interval_name)
                x = finalize_scoring(enhance_guard(x))
                x = post_process_summary(x)

                logger.info(
                    "[summary_runtime] post-seed MTF rebuild fallback done tf=%s rows=%d symbols=%d "
                    "after_mtf=%d after_score_mtf=%d after_mtf_score=%d",
                    tf,
                    len(x),
                    safe_symbols_count(x),
                    nonzero_count(x, "mtf"),
                    nonzero_count(x, "score_mtf"),
                    nonzero_count(x, "mtf_score"),
                )
                return x

    except Exception:
        logger.debug(
            "[summary_runtime] post-seed MTF rebuild fallback failed tf=%s",
            tf,
            exc_info=True,
        )

    logger.warning(
        "[summary_runtime] post-seed MTF rebuild could not improve tf=%s rows=%d symbols=%d",
        tf,
        len(df),
        safe_symbols_count(df),
    )
    return df


__all__ = [
    "call_with_supported_kwargs",
    "maybe_rebuild_seed_indicators",
    "need_post_seed_mtf_rebuild",
    "resolve_optional_callable",
    "post_seed_mtf_rebuild",
]