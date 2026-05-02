# ============================================================
# File   : trading/summary/engine/ranking_summary_engine_adapter.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-ENGINE-ADAPTER
# ------------------------------------------------------------
# ✔ ランキング由来サマリー専用の薄い adapter
# ✔ push 系ロジックは一切持たない
# ✔ ranking runner / ranking engine に委譲
# ✔ ranking_summary_* の保存専用
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from global_state import global_data
except Exception:
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None


def _resolve_callable(candidates: list[tuple[str, str]]) -> Optional[Callable]:
    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info("[RANKING SUMMARY ADAPTER] resolved %s -> %s.%s", func_name, module_name, func_name)
                return fn
        except Exception as e:
            logger.warning(
                "[RANKING SUMMARY ADAPTER] candidate import failed: %s.%s: %s: %s",
                module_name, func_name, type(e).__name__, e,
            )
    return None


def _resolve_ranking_summary_fn() -> Optional[Callable]:
    return _resolve_callable([
        ("trading.summary.engine.ranking_summary_engine", "build_ranking_summary"),
        ("trading.summary.engine.ranking_summary_engine", "run_ranking_summary"),
        ("trading.summary.engine.ranking_summary_engine", "job_ranking_summary"),
        ("trading.ranking.ranking_summary_engine", "run_ranking_summary_job"),
        ("trading.ranking.ranking_summary_engine", "run_ranking_summary"),
    ])


def _safe_copy_df(value) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()

    if isinstance(value, tuple) and len(value) >= 1 and isinstance(value[0], pd.DataFrame):
        return value[0].copy()

    if isinstance(value, dict):
        for key in ("result_df", "merged_df", "df", "summary_df", "output_df", "display_df"):
            v = value.get(key)
            if isinstance(v, pd.DataFrame):
                return v.copy()

    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _store_ranking_frame(interval: int, ranking_df: pd.DataFrame) -> None:
    if global_data is None:
        return

    for name in (
        f"ranking_summary_{interval}min",
        f"ranking_summary_{interval}",
    ):
        try:
            setattr(global_data, name, ranking_df.copy())
        except Exception:
            logger.exception("[RANKING SUMMARY ADAPTER] store failed key=%s interval=%s", name, interval)

    setter = getattr(global_data, "set_ranking_summary", None)
    if callable(setter):
        try:
            setter(interval, ranking_df.copy())
        except Exception:
            logger.exception("[RANKING SUMMARY ADAPTER] set_ranking_summary failed interval=%s", interval)


def build_ranking_summary(interval: int = 1) -> pd.DataFrame:
    logger.info("🚀 ranking_summary_engine_adapter START interval=%s", interval)

    fn = _resolve_ranking_summary_fn()
    if fn is None:
        logger.warning("[RANKING SUMMARY ADAPTER] ranking summary fn unavailable")
        return pd.DataFrame()

    try:
        out = fn(interval=interval, announce=False, use_discord=False)
    except TypeError:
        try:
            out = fn(interval=interval)
        except Exception:
            logger.exception("[RANKING SUMMARY ADAPTER] ranking summary failed interval=%s", interval)
            return pd.DataFrame()
    except Exception:
        logger.exception("[RANKING SUMMARY ADAPTER] ranking summary failed interval=%s", interval)
        return pd.DataFrame()

    df = _safe_copy_df(out)
    _store_ranking_frame(interval, df)

    logger.info(
        "✅ ranking_summary_engine_adapter END interval=%s rows=%s",
        interval,
        len(df),
    )
    return df


def run_ranking_summary_engine(interval: int = 1) -> pd.DataFrame:
    return build_ranking_summary(interval=interval)


def run(interval: int = 1) -> pd.DataFrame:
    return build_ranking_summary(interval=interval)