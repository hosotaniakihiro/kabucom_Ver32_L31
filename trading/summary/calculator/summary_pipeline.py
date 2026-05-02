
# ============================================================
# File   : trading/summary/calculator/summary_pipeline.py
# Version: Ver32-L01-PRODUCTION-COMPAT-SUMMARY-PIPELINE-WRAPPER
# ------------------------------------------------------------
# 機能:
#   - summary pipeline calculator wrapper
#   - 旧 calculate_summary / 新 run_summary_pipeline 両対応
#   - import 失敗時の安全化
#   - runtime compatibility shim
# ------------------------------------------------------------
# 対応方針:
#   1. trading.summary.pipeline.summary_pipeline から
#      利用可能な実装を動的に解決する
#   2. 優先順位:
#         run_summary_pipeline
#         run_summary_job
#         calculate_summary
#   3. どれも無い場合は空DataFrameを返して落ちない
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PIPELINE_FN: Optional[Callable[..., Any]] = None
_IMPORT_ERROR: Optional[Exception] = None


# ============================================================
# internal
# ============================================================

def _resolve_pipeline_function() -> Optional[Callable[..., Any]]:
    """
    summary pipeline 実装を動的解決する。
    優先順位:
        1) run_summary_pipeline
        2) run_summary_job
        3) calculate_summary
    """
    global _PIPELINE_FN, _IMPORT_ERROR

    if _PIPELINE_FN is not None:
        return _PIPELINE_FN

    try:
        mod = importlib.import_module("trading.summary.pipeline.summary_pipeline")
    except Exception as e:
        _IMPORT_ERROR = e
        logger.exception(
            "[SUMMARY PIPELINE CALCULATOR WRAPPER] pipeline import failed"
        )
        return None

    candidates = [
        "run_summary_pipeline",
        "run_summary_job",
        "calculate_summary",
    ]

    for name in candidates:
        fn = getattr(mod, name, None)
        if callable(fn):
            _PIPELINE_FN = fn
            logger.info(
                "[SUMMARY PIPELINE CALCULATOR WRAPPER] resolved implementation -> %s.%s",
                mod.__name__,
                name,
            )
            return _PIPELINE_FN

    logger.error(
        "[SUMMARY PIPELINE CALCULATOR WRAPPER] no callable implementation found in %s candidates=%s",
        mod.__name__,
        candidates,
    )
    return None


def _ensure_dataframe(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()

    if isinstance(obj, pd.DataFrame):
        return obj.copy()

    try:
        return pd.DataFrame(obj).copy()
    except Exception:
        logger.exception(
            "[SUMMARY PIPELINE CALCULATOR WRAPPER] dataframe conversion failed type=%s",
            type(obj).__name__,
        )
        return pd.DataFrame()


def _call_best_effort(
        fn: Callable[..., Any],
        summary_df: Optional[pd.DataFrame] = None,
        push_df: Optional[pd.DataFrame] = None,
        interval: int | str = 1,
        **kwargs,
) -> pd.DataFrame:
    """
    実装ごとのシグネチャ差異を吸収して best effort 呼び出しする。
    """

    attempts = [
        lambda: fn(
            summary_df=summary_df,
            push_df=push_df,
            interval=interval,
            **kwargs,
        ),
        lambda: fn(
            push_df=push_df,
            interval=interval,
            **kwargs,
        ),
        lambda: fn(
            summary_df,
            push_df,
            interval=interval,
            **kwargs,
        ),
        lambda: fn(
            push_df,
            interval=interval,
            **kwargs,
        ),
        lambda: fn(
            interval=interval,
            **kwargs,
        ),
        lambda: fn(),
    ]

    last_error = None

    for idx, attempt in enumerate(attempts, start=1):
        try:
            out = attempt()
            return _ensure_dataframe(out)
        except TypeError as e:
            last_error = e
            logger.debug(
                "[SUMMARY PIPELINE CALCULATOR WRAPPER] signature attempt failed attempt=%s err=%s",
                idx,
                e,
            )
        except Exception:
            logger.exception(
                "[SUMMARY PIPELINE CALCULATOR WRAPPER] pipeline execution failed attempt=%s",
                idx,
            )
            return pd.DataFrame()

    logger.error(
        "[SUMMARY PIPELINE CALCULATOR WRAPPER] all call attempts failed last_error=%s",
        last_error,
    )
    return pd.DataFrame()


# ============================================================
# public
# ============================================================

def calculate_summary(
        summary_df: Optional[pd.DataFrame] = None,
        push_df: Optional[pd.DataFrame] = None,
        interval: int | str = 1,
        **kwargs,
) -> pd.DataFrame:
    """
    旧呼び出し互換 API。
    外部から calculate_summary() で呼ばれても内部で新実装へ中継する。
    """
    fn = _resolve_pipeline_function()
    if fn is None:
        return pd.DataFrame()

    return _call_best_effort(
        fn=fn,
        summary_df=summary_df,
        push_df=push_df,
        interval=interval,
        **kwargs,
    )


def run_summary_pipeline(
        summary_df: Optional[pd.DataFrame] = None,
        push_df: Optional[pd.DataFrame] = None,
        interval: int | str = 1,
        **kwargs,
) -> pd.DataFrame:
    """
    新名称互換 API。
    """
    return calculate_summary(
        summary_df=summary_df,
        push_df=push_df,
        interval=interval,
        **kwargs,
    )