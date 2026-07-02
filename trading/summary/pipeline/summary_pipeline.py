# ============================================================
# File   : trading/summary/pipeline/summary_pipeline.py
# Version: Ver32_L08-SIGNAL-OUTPUT-TODAY-GUARD
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
#   - push_df が空で summary_df がある場合は、空DFで既存PUSHサマリーを上書きしない
#   - 前日 summary は MA/ATR/RSI 等の履歴計算には使うが、entry/AI/表示用の
#     signal output には当日行だけを渡す
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


def _reuse_summary_when_push_empty() -> bool:
    """push_df が一時的に空の時、既存 summary_df を返してサマリー0行上書きを防ぐ。"""
    return _env_bool("AUTOSTOCK_REUSE_SUMMARY_ON_EMPTY_PUSH", True)


def _signal_output_today_only() -> bool:
    """
    entry / AI / Discord表示に使う signal output だけを当日行へ制限する。

    前日 summary は MA/ATR/RSI などの履歴計算に必要なので、indicator enrich 前には
    落とさない。計算後、evaluate_signals=True の出口だけで当日行へ絞る。
    """
    return _env_bool("AUTOSTOCK_SIGNAL_OUTPUT_TODAY_ONLY", True)


def _resolve_trade_date():
    """当日判定に使う trade_date。通常はローカル日付。テスト時のみ env で固定可。"""
    raw = os.environ.get("AUTOSTOCK_TRADE_DATE") or os.environ.get("TRADE_DATE")
    if raw:
        try:
            return pd.Timestamp(str(raw).strip()).date()
        except Exception:
            pass
    try:
        return pd.Timestamp.now().date()
    except Exception:
        return None


def _primary_datetime_col(df: pd.DataFrame) -> str | None:
    for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time", "received_at"):
        if c in df.columns:
            return c
    return None


def _filter_today_for_signal_output(
    df: pd.DataFrame,
    *,
    interval: int,
    evaluate_signals: bool,
) -> pd.DataFrame:
    """履歴計算後、entry/AI/表示用の出力から前日行を除外する。"""
    out = ensure_dataframe(df, "signal_output_today_filter")
    if out.empty:
        return out
    if not evaluate_signals or not _signal_output_today_only():
        return out

    trade_date = _resolve_trade_date()
    if trade_date is None:
        return out

    out = coerce_datetime_columns(out)
    dt_col = _primary_datetime_col(out)
    if not dt_col:
        logger.warning(
            "[summary_pipeline] signal today guard skipped no datetime column interval=%s rows=%s cols=%s",
            interval,
            len(out),
            list(out.columns)[:20],
        )
        return out

    try:
        dt = pd.to_datetime(out[dt_col], errors="coerce")
        mask = dt.dt.date == trade_date
    except Exception as e:
        logger.warning(
            "[summary_pipeline] signal today guard failed interval=%s err=%s: %s",
            interval,
            type(e).__name__,
            str(e)[:200],
            exc_info=False,
        )
        return out

    before = len(out)
    before_symbols = safe_symbols(out)
    before_latest = safe_latest_dt(out)
    kept = out.loc[mask].copy()

    if before != len(kept):
        logger.warning(
            "[summary_pipeline] signal today guard interval=%s trade_date=%s before=%s after=%s "
            "symbols_before=%s symbols_after=%s latest_before=%s latest_after=%s evaluate_signals=%s",
            interval,
            trade_date,
            before,
            len(kept),
            before_symbols,
            safe_symbols(kept),
            before_latest,
            safe_latest_dt(kept),
            evaluate_signals,
        )

    if kept.empty:
        logger.warning(
            "[summary_pipeline] signal today guard empty interval=%s trade_date=%s latest_before=%s. "
            "Previous-day summary remains usable only as indicator history, not as entry/AI/display output.",
            interval,
            trade_date,
            before_latest,
        )

    return kept.reset_index(drop=True)


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
    evaluate_signals: bool = True,
) -> pd.DataFrame:
    out = ensure_dataframe(df, "postprocess")
    if out.empty:
        return out

    out = coerce_datetime_columns(out)

    logger.info(
        "[summary_pipeline] postprocess pre latest_only=%s interval=%s rows=%s symbols=%s latest_dt=%s evaluate_signals=%s",
        latest_only,
        interval,
        len(out),
        safe_symbols(out),
        safe_latest_dt(out),
        evaluate_signals,
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

    # 重要:
    #   ここまでは前日 summary を含めてよい。MA/ATR/RSI 等の履歴計算に必要。
    #   ただし entry / AI / Discord表示に渡す signal output は当日行のみ。
    out = _filter_today_for_signal_output(
        out,
        interval=interval,
        evaluate_signals=evaluate_signals,
    )

    if out.empty:
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


def _reuse_existing_summary_output(
    summary_df: pd.DataFrame,
    *,
    interval: int,
    latest_only: bool,
    evaluate_signals: bool,
    reason: str,
) -> pd.DataFrame:
    logger.warning(
        "[summary_pipeline] reuse existing summary interval=%s reason=%s rows=%s symbols=%s latest_dt=%s latest_only=%s evaluate_signals=%s",
        interval,
        reason,
        len(summary_df),
        safe_symbols(summary_df),
        safe_latest_dt(summary_df),
        latest_only,
        evaluate_signals,
    )
    return _postprocess_output(
        summary_df,
        interval=interval,
        latest_only=latest_only,
        evaluate_signals=evaluate_signals,
    )


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

        if len(push_df2) == 0 and len(summary_df2) > 0 and _reuse_summary_when_push_empty():
            return _reuse_existing_summary_output(
                summary_df2,
                interval=interval_n,
                latest_only=latest_only,
                evaluate_signals=evaluate_signals,
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
                    if len(out) == 0 and len(push_df2) == 0 and len(summary_df2) > 0 and _reuse_summary_when_push_empty():
                        return _reuse_existing_summary_output(
                            summary_df2,
                            interval=interval_n,
                            latest_only=latest_only,
                            evaluate_signals=evaluate_signals,
                            reason=f"candidate_empty:{getattr(fn, '__name__', '?')}",
                        )

                    post = _postprocess_output(
                        out,
                        interval=interval_n,
                        latest_only=latest_only,
                        evaluate_signals=evaluate_signals,
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
                            "[summary_pipeline] output became empty after postprocess fn=%s interval=%s raw_rows=%s latest_only=%s evaluate_signals=%s",
                            getattr(fn, "__name__", "?"),
                            interval_n,
                            len(out),
                            latest_only,
                            evaluate_signals,
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
                evaluate_signals=evaluate_signals,
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
