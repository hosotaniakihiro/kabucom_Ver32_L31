# ============================================================
# File   : trading/summary/pipeline/summary_pipeline.py
# Version: Ver32_L06-SPLIT-PUSH-SUMMARY-PIPELINE-MAIN-EMPTY-FASTRETURN
# ------------------------------------------------------------
# Purpose:
#   PUSH/Yahoo由来 stock_summary 用 pipeline 入口。
#
# Important:
#   - ranking_summary 用ではない
#   - ranking_snapshot 由来の擬似OHLCには本物ATRを計算しない
#   - ATR / slope / slope_atr_scaled は indicator_enrich 経由で
#     trading.summary.indicators.atr_slope_safe を優先使用
#   - main.py で summary/push が両方空の場合は legacy candidate を呼ばず空DFを返す
#     （NAS SQLite/cache fallback 経由の Windows 0xC0000006 を避ける）
# ============================================================

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import pandas as pd

from .dataframe_safe import (
    coerce_datetime_columns,
    ensure_dataframe,
    latest_only as latest_only_df,
    safe_latest_dt,
    safe_symbols,
)
from .indicator_enrich import (
    force_enrich_indicators,
    log_indicator_profile,
    should_enrich_indicators,
)
from .legacy_candidates import (
    try_incremental_engine,
    try_summary_controller,
    try_summary_engine,
)
from .trade_universe_filter import (
    apply_pipeline_common_trade_universe_filter,
    resolve_pipeline_min_price,
)

logger = logging.getLogger(__name__)
_PIPELINE_GUARD = threading.local()


def _normalize_interval(interval: int | str = 1) -> int:
    try:
        s = str(interval).strip().lower().replace(" ", "")
        if s.endswith("min"):
            s = s[:-3]
        n = int(s)
        return n if n > 0 else 1
    except Exception:
        return 1


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_main_py_process() -> bool:
    try:
        return Path(sys.argv[0]).name.lower() == "main.py"
    except Exception:
        return False


def _main_skip_empty_pipeline_candidates() -> bool:
    """
    main.py 起動直後、PUSH stack / PUSH fallback / ranking bootstrap を止めた状態では
    summary_df / push_df が両方空になる。この状態で legacy candidate を呼ぶと
    summary_controller / engine 側が NAS SQLite / cache を読みに行くため、
    Windows 0xC0000006 の再発点になる。

    main.py では既定で空DFを即返し、DB作成・復元・計算は main_database.py に任せる。
    """
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_EMPTY_SUMMARY_PIPELINE", True)


def _guard_get_depth() -> int:
    try:
        return int(getattr(_PIPELINE_GUARD, "depth", 0))
    except Exception:
        return 0


def _guard_push() -> int:
    depth = _guard_get_depth() + 1
    _PIPELINE_GUARD.depth = depth
    return depth


def _guard_pop() -> int:
    depth = max(_guard_get_depth() - 1, 0)
    _PIPELINE_GUARD.depth = depth
    return depth


def _postprocess_output(
    df: pd.DataFrame,
    *,
    interval: int,
    latest_only: bool,
) -> pd.DataFrame:
    out = ensure_dataframe(df, "postprocess")
    if out.empty:
        return out

    out = coerce_datetime_columns(out)

    logger.info(
        "[summary_pipeline] postprocess pre latest_only=%s interval=%s rows=%s symbols=%s latest_dt=%s",
        latest_only,
        interval,
        len(out),
        safe_symbols(out),
        safe_latest_dt(out),
    )

    out = apply_pipeline_common_trade_universe_filter(
        out,
        interval=interval,
        context="postprocess_before_indicator",
    )

    if out.empty:
        logger.warning(
            "[summary_pipeline] postprocess empty after price filter interval=%s",
            interval,
        )
        return out

    if should_enrich_indicators(out):
        logger.warning(
            "[summary_pipeline] indicator enrich start interval=%s rows=%s symbols=%s latest_dt=%s",
            interval,
            len(out),
            safe_symbols(out),
            safe_latest_dt(out),
        )
        out = force_enrich_indicators(out, interval=interval)

    out = apply_pipeline_common_trade_universe_filter(
        out,
        interval=interval,
        context="postprocess_after_indicator",
    )

    if out.empty:
        logger.warning(
            "[summary_pipeline] postprocess empty after indicator price filter interval=%s",
            interval,
        )
        return out

    log_indicator_profile(f"POST-BEFORE-LATEST-{interval}m", out)

    if latest_only:
        before_latest = len(out)
        out = latest_only_df(out)
        logger.info(
            "[summary_pipeline] post latest_only interval=%s before=%s after=%s symbols=%s latest_dt=%s",
            interval,
            before_latest,
            len(out),
            safe_symbols(out),
            safe_latest_dt(out),
        )

    out = apply_pipeline_common_trade_universe_filter(
        out,
        interval=interval,
        context="postprocess_after_latest",
    )

    log_indicator_profile(f"POST-AFTER-LATEST-{interval}m", out)

    logger.info(
        "[summary_pipeline] postprocess done interval=%s rows=%s cols=%s symbols=%s latest_dt=%s",
        interval,
        len(out),
        len(out.columns),
        safe_symbols(out),
        safe_latest_dt(out),
    )

    return out.reset_index(drop=True)


def run_summary_pipeline(
    summary_df: Optional[pd.DataFrame] = None,
    push_df: Optional[pd.DataFrame] = None,
    *,
    interval: int | str = 1,
    evaluate_signals: bool = True,
    latest_only: bool = True,
    recent_bars_per_symbol: int = 120,
) -> pd.DataFrame:
    """
    PUSH/Yahoo由来 stock_summary 用 pipeline。

    注意:
      ranking_summary 用ではない。
      ranking_snapshot 由来の擬似OHLCには本物ATRを計算しない。
    """
    depth = _guard_push()

    try:
        interval_n = _normalize_interval(interval)

        summary_df2 = ensure_dataframe(summary_df, "summary_df")
        push_df2 = ensure_dataframe(push_df, "push_df")

        logger.info(
            "[summary_pipeline] start interval=%s summary_rows=%s summary_symbols=%s summary_latest_dt=%s "
            "push_rows=%s push_symbols=%s push_latest_dt=%s evaluate_signals=%s latest_only=%s "
            "recent_bars_per_symbol=%s depth=%s min_price=%.1f",
            interval_n,
            len(summary_df2),
            safe_symbols(summary_df2),
            safe_latest_dt(summary_df2),
            len(push_df2),
            safe_symbols(push_df2),
            safe_latest_dt(push_df2),
            evaluate_signals,
            latest_only,
            recent_bars_per_symbol,
            depth,
            resolve_pipeline_min_price(),
        )

        if len(summary_df2) == 0 and len(push_df2) == 0 and _main_skip_empty_pipeline_candidates():
            logger.warning(
                "[summary_pipeline] main.py empty input fast-return interval=%s summary_rows=0 push_rows=0 "
                "skip legacy candidates to avoid NAS SQLite/cache fallback 0xC0000006. "
                "main_database.py handles summary calculation/cache. "
                "Set AUTOSTOCK_MAIN_SKIP_EMPTY_SUMMARY_PIPELINE=0 to restore legacy behavior.",
                interval_n,
            )
            return pd.DataFrame()

        candidates = (
            try_incremental_engine,
            try_summary_controller,
            try_summary_engine,
        )

        # 再入時は summary_engine だけ避ける
        if depth >= 1:
            candidates = (
                try_incremental_engine,
                try_summary_controller,
            )

        for fn in candidates:
            try:
                out = fn(
                    summary_df2,
                    push_df2,
                    interval=interval_n,
                    evaluate_signals=evaluate_signals,
                    latest_only=latest_only,
                    recent_bars_per_symbol=recent_bars_per_symbol,
                )

                if isinstance(out, pd.DataFrame):
                    post = _postprocess_output(
                        out,
                        interval=interval_n,
                        latest_only=latest_only,
                    )

                    logger.info(
                        "[summary_pipeline] candidate success fn=%s interval=%s raw_rows=%s post_rows=%s raw_latest_dt=%s post_latest_dt=%s",
                        getattr(fn, "__name__", "?"),
                        interval_n,
                        len(out),
                        len(post),
                        safe_latest_dt(out),
                        safe_latest_dt(post),
                    )

                    if len(post) == 0 and len(out) > 0:
                        logger.warning(
                            "[summary_pipeline] output became empty after postprocess fn=%s interval=%s raw_rows=%s latest_only=%s",
                            getattr(fn, "__name__", "?"),
                            interval_n,
                            len(out),
                            latest_only,
                        )

                    return post

            except Exception as e:
                logger.error(
                    "[summary_pipeline] candidate wrapper failed fn=%s interval=%s err=%s: %s",
                    getattr(fn, "__name__", "?"),
                    interval_n,
                    type(e).__name__,
                    str(e)[:300],
                    exc_info=False,
                )

        logger.warning(
            "[summary_pipeline] no candidate succeeded interval=%s summary_rows=%s push_rows=%s -> return empty",
            interval_n,
            len(summary_df2),
            len(push_df2),
        )

        if len(push_df2) > 0:
            logger.warning(
                "[summary_pipeline][ZERO-ROWS-TRACE] interval=%s push exists but no candidate produced output "
                "push_symbols=%s push_latest_dt=%s",
                interval_n,
                safe_symbols(push_df2),
                safe_latest_dt(push_df2),
            )

        return pd.DataFrame()

    except Exception as e:
        logger.error(
            "[summary_pipeline] run_summary_pipeline failed interval=%r err=%s: %s",
            interval,
            type(e).__name__,
            str(e)[:300],
            exc_info=False,
        )
        return pd.DataFrame()

    finally:
        _guard_pop()


__all__ = ["run_summary_pipeline"]
