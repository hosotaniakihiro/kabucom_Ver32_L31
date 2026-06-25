# ============================================================
# File   : trading/summary/pipeline/summary_pipeline.py
# Version: Ver32_L08-BLOCK-FUTURE-REUSED-SUMMARY
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
#   - push_df が空で summary_df がある場合でも、summary_df が未来時刻なら再利用しない
# ============================================================

from __future__ import annotations

import datetime as dt
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


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


def _reuse_summary_when_push_empty() -> bool:
    """push_df が一時的に空の時、既存 summary_df を返してサマリー0行上書きを防ぐ。"""
    return _env_bool("AUTOSTOCK_REUSE_SUMMARY_ON_EMPTY_PUSH", True)


def _future_tolerance_sec() -> float:
    return max(0.0, _env_float("SUMMARY_PIPELINE_FUTURE_TOLERANCE_SEC", 60.0))


def _latest_dt_from_df(df: pd.DataFrame) -> dt.datetime | None:
    try:
        if df is None or df.empty:
            return None
        col = None
        for c in ("datetime", "dt", "end_time", "start_time", "time", "snapshot_time"):
            if c in df.columns:
                col = c
                break
        if not col:
            return None
        s = pd.to_datetime(df[col], errors="coerce").dropna()
        if s.empty:
            return None
        return s.max().to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


def _is_future_df(df: pd.DataFrame, *, label: str, interval: int, reason: str) -> bool:
    if not _env_bool("SUMMARY_PIPELINE_BLOCK_FUTURE_SUMMARY", True):
        return False
    latest = _latest_dt_from_df(df)
    if latest is None:
        return False
    now = dt.datetime.now()
    tol = _future_tolerance_sec()
    if latest > now + dt.timedelta(seconds=tol):
        logger.warning(
            "[summary_pipeline] future summary blocked label=%s interval=%s reason=%s latest_dt=%s now=%s future_sec=%.1f tolerance=%.1f rows=%s symbols=%s",
            label,
            interval,
            reason,
            latest,
            now,
            (latest - now).total_seconds(),
            tol,
            len(df),
            safe_symbols(df),
        )
        return True
    return False


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
    if _is_future_df(out, label="postprocess", interval=interval, reason="candidate_output"):
        return pd.DataFrame()

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
        if _is_future_df(out, label="postprocess_after_indicator", interval=interval, reason="indicator_enrich_output"):
            return pd.DataFrame()

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
        if _is_future_df(out, label="postprocess_after_latest", interval=interval, reason="latest_only_output"):
            return pd.DataFrame()

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


def _reuse_existing_summary_output(
    summary_df: pd.DataFrame,
    *,
    interval: int,
    latest_only: bool,
    reason: str,
) -> pd.DataFrame:
    if _is_future_df(summary_df, label="reuse_existing", interval=interval, reason=reason):
        logger.warning(
            "[summary_pipeline] reuse existing summary skipped because future-dated interval=%s reason=%s rows=%s latest_dt=%s",
            interval,
            reason,
            len(summary_df),
            safe_latest_dt(summary_df),
        )
        return pd.DataFrame()
    logger.warning(
        "[summary_pipeline] reuse existing summary interval=%s reason=%s rows=%s symbols=%s latest_dt=%s latest_only=%s",
        interval,
        reason,
        len(summary_df),
        safe_symbols(summary_df),
        safe_latest_dt(summary_df),
        latest_only,
    )
    return _postprocess_output(summary_df, interval=interval, latest_only=latest_only)


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

        if len(summary_df2) > 0 and _is_future_df(summary_df2, label="input_summary", interval=interval_n, reason="input_check"):
            summary_df2 = pd.DataFrame()
        if len(push_df2) > 0 and _is_future_df(push_df2, label="input_push", interval=interval_n, reason="input_check"):
            push_df2 = pd.DataFrame()

        if len(summary_df2) == 0 and len(push_df2) == 0 and _main_skip_empty_pipeline_candidates():
            logger.warning(
                "[summary_pipeline] main.py empty input fast-return interval=%s summary_rows=0 push_rows=0 "
                "skip legacy candidates to avoid NAS SQLite/cache fallback 0xC0000006. "
                "main_database.py handles summary calculation/cache. "
                "Set AUTOSTOCK_MAIN_SKIP_EMPTY_SUMMARY_PIPELINE=0 to restore legacy behavior.",
                interval_n,
            )
            return pd.DataFrame()

        if len(push_df2) == 0 and len(summary_df2) > 0 and _reuse_summary_when_push_empty():
            return _reuse_existing_summary_output(
                summary_df2,
                interval=interval_n,
                latest_only=latest_only,
                reason="push_df_empty",
            )

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
                    if _is_future_df(out, label=f"candidate:{getattr(fn, '__name__', '?')}", interval=interval_n, reason="candidate_raw"):
                        logger.warning(
                            "[summary_pipeline] candidate output skipped because future-dated fn=%s interval=%s raw_rows=%s raw_latest_dt=%s",
                            getattr(fn, "__name__", "?"),
                            interval_n,
                            len(out),
                            safe_latest_dt(out),
                        )
                        continue

                    if len(out) == 0 and len(push_df2) == 0 and len(summary_df2) > 0 and _reuse_summary_when_push_empty():
                        return _reuse_existing_summary_output(
                            summary_df2,
                            interval=interval_n,
                            latest_only=latest_only,
                            reason=f"candidate_empty:{getattr(fn, '__name__', '?')}",
                        )

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

        if len(summary_df2) > 0 and _reuse_summary_when_push_empty():
            return _reuse_existing_summary_output(
                summary_df2,
                interval=interval_n,
                latest_only=latest_only,
                reason="no_candidate_succeeded",
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
