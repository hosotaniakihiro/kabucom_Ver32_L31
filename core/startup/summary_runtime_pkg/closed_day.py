# ============================================================
# File   : core/startup/summary_runtime_pkg/closed_day.py
# Version: REV3.0-SUMMARY-RUNTIME-CLOSED-DAY
# ------------------------------------------------------------
# 【概要】
#   closed-day display fallback / rebuild 管理
#
# 【主な機能】
#   - 既存 merged summary が ready なら再計算しない
#   - cache が空なら summary DB seed
#   - closed_day_recalc が無い場合の fallback
#   - keep-existing guard
# ============================================================

from __future__ import annotations

import logging

from global_state import global_data

from trading.summary.summary_post_processor import post_process_summary
from trading.summary.engine.processors.mtf import safe_mtf
from trading.summary.engine.processors.scoring import safe_scoring
from trading.summary.engine.guards.enhance_guard import enhance_guard
from trading.summary.engine.internal.scoring_guard import finalize_scoring

from core.startup.display_debug import log_closed_day_summary_state
from core.startup.merged_summary_access import (
    get_push_merged_summary_safe,
    set_push_merged_summary_safe,
)

from .dataframe_utils import (
    is_nonempty_df,
    can_skip_closed_day_recalc,
    normalize_symbol_local,
    iter_symbols_from_any,
)
from .db_seed import seed_runtime_summary_cache_from_db

logger = logging.getLogger(__name__)

_CLOSED_DAY_RECALC_AVAILABLE = False
_rebuild_closed_day_all = None

try:
    from trading.summary.closed_day_recalc import (
        rebuild_closed_day_all as _rebuild_closed_day_all,
    )

    _CLOSED_DAY_RECALC_AVAILABLE = True
except Exception:
    _CLOSED_DAY_RECALC_AVAILABLE = False
    _rebuild_closed_day_all = None


def safe_set_push_merged(tf: int, df) -> None:
    try:
        if df is None or getattr(df, "empty", True):
            return
        set_push_merged_summary_safe(tf, df)
    except Exception:
        logger.debug("set_push_merged_summary_safe failed tf=%s", tf, exc_info=True)


def safe_get_push_merged(tf: int):
    try:
        return get_push_merged_summary_safe(tf)
    except Exception:
        logger.debug("get_push_merged_summary_safe failed tf=%s", tf, exc_info=True)
        return None


def rebuild_closed_day_summary_for_display_fallback(df, tf: int):
    if df is None or df.empty:
        return df

    try:
        interval_name = f"{int(tf)}min"

        log_closed_day_summary_state(f"{interval_name}-before-enhance", df)
        df = enhance_guard(df)
        log_closed_day_summary_state(f"{interval_name}-after-enhance", df)

        df = safe_mtf(df)
        log_closed_day_summary_state(f"{interval_name}-after-mtf", df)

        df = safe_scoring(df, interval_name)
        log_closed_day_summary_state(f"{interval_name}-after-scoring", df)

        df = finalize_scoring(enhance_guard(df))
        log_closed_day_summary_state(f"{interval_name}-after-finalize", df)

        df = post_process_summary(df)
        log_closed_day_summary_state(f"{interval_name}-after-post-process", df)
        return df

    except Exception:
        logger.exception("[CLOSED DAY] fallback rebuild failed tf=%s", tf)
        try:
            return post_process_summary(df)
        except Exception:
            logger.exception("[CLOSED DAY] fallback post_process failed tf=%s", tf)
            return df


def collect_closed_day_priority_symbols(limit_symbols: int = 300) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    candidate_attrs = [
        "active_symbols",
        "monitor_symbols",
        "buy_candidate_symbols",
        "sell_candidate_symbols",
        "push_symbols",
        "runtime_symbols",
        "ranking_summary_universe",
        "daily_watchlist_symbols",
        "daily_watchlist",
        "ats_register_targets",
        "ats_targets",
    ]

    for attr in candidate_attrs:
        try:
            vals = getattr(global_data, attr, None)
        except Exception:
            vals = None

        for s in iter_symbols_from_any(vals):
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
            if len(out) >= limit_symbols:
                return out

    return out


def maybe_prepare_closed_day_display_cache() -> None:
    try:
        df1 = safe_get_push_merged(1)
        df3 = safe_get_push_merged(3)
        df5 = safe_get_push_merged(5)

        if can_skip_closed_day_recalc(df1, df3, df5):
            logger.info("[CLOSED DAY] existing merged summaries look ready -> skip fallback rebuild")
            return

        if not is_nonempty_df(df1) and not is_nonempty_df(df3) and not is_nonempty_df(df5):
            seed_runtime_summary_cache_from_db(
                force=True,
                stage="closed-day-display-cache-empty",
                rebuild_missing_scores=True,
            )
            df1 = safe_get_push_merged(1)
            df3 = safe_get_push_merged(3)
            df5 = safe_get_push_merged(5)

            if can_skip_closed_day_recalc(df1, df3, df5):
                logger.info("[CLOSED DAY] DB seeded merged summaries ready -> skip fallback rebuild")
                return

        for tf, df in ((1, df1), (3, df3), (5, df5)):
            if df is None or getattr(df, "empty", True):
                continue
            rebuilt = rebuild_closed_day_summary_for_display_fallback(df, tf)
            safe_set_push_merged(tf, rebuilt)

    except Exception:
        logger.exception("[CLOSED DAY] maybe_prepare_closed_day_display_cache failed")


def limit_closed_day_df(df, symbols: list[str]):
    if df is None or df.empty:
        return df
    if not symbols:
        return df

    try:
        if "symbol" not in df.columns:
            return df

        x = df.copy()
        x["symbol_norm"] = x["symbol"].astype(str).map(normalize_symbol_local)

        symbol_set = {normalize_symbol_local(s) for s in symbols if normalize_symbol_local(s)}
        common = set(x["symbol_norm"].dropna().astype(str).tolist()) & symbol_set

        if not common:
            return x.drop(columns=["symbol_norm"], errors="ignore")

        out = x[x["symbol_norm"].isin(common)].copy()
        return out.drop(columns=["symbol_norm"], errors="ignore")
    except Exception:
        logger.exception("[CLOSED DAY] priority df filter failed")
        return df


def keep_existing_when_empty(new_df, existing_df, tf: int, stage: str):
    try:
        new_empty = (new_df is None) or (hasattr(new_df, "empty") and new_df.empty)
        old_empty = (existing_df is None) or (hasattr(existing_df, "empty") and existing_df.empty)

        if new_empty and not old_empty:
            logger.warning(
                "[CLOSED DAY] keep existing summary because recalc/post result empty tf=%s stage=%s",
                tf,
                stage,
            )
            return existing_df
        return new_df
    except Exception:
        logger.exception("[CLOSED DAY] keep-existing guard failed tf=%s stage=%s", tf, stage)
        return existing_df if existing_df is not None else new_df


def rebuild_closed_day_summaries_all(lightweight: bool = True, limit_symbols: int = 300):
    df1 = get_push_merged_summary_safe(1)
    df3 = get_push_merged_summary_safe(3)
    df5 = get_push_merged_summary_safe(5)

    if not is_nonempty_df(df1) and not is_nonempty_df(df3) and not is_nonempty_df(df5):
        seed_runtime_summary_cache_from_db(
            force=True,
            stage="closed-day-rebuild-empty-cache",
            rebuild_missing_scores=True,
        )
        df1 = get_push_merged_summary_safe(1)
        df3 = get_push_merged_summary_safe(3)
        df5 = get_push_merged_summary_safe(5)

    log_closed_day_summary_state("1min-loaded", df1)
    log_closed_day_summary_state("3min-loaded", df3)
    log_closed_day_summary_state("5min-loaded", df5)

    if can_skip_closed_day_recalc(df1, df3, df5):
        logger.info("[CLOSED DAY] recalc skipped: restored snapshots already have ready scores")
        return {
            1: post_process_summary(df1) if df1 is not None and not df1.empty else df1,
            3: post_process_summary(df3) if df3 is not None and not df3.empty else df3,
            5: post_process_summary(df5) if df5 is not None and not df5.empty else df5,
        }

    if not _CLOSED_DAY_RECALC_AVAILABLE or _rebuild_closed_day_all is None:
        logger.warning("[CLOSED DAY] closed_day_recalc unavailable -> fallback path")
        return {
            1: rebuild_closed_day_summary_for_display_fallback(df1, 1) if df1 is not None and not df1.empty else df1,
            3: rebuild_closed_day_summary_for_display_fallback(df3, 3) if df3 is not None and not df3.empty else df3,
            5: rebuild_closed_day_summary_for_display_fallback(df5, 5) if df5 is not None and not df5.empty else df5,
        }

    try:
        target_symbols = collect_closed_day_priority_symbols(limit_symbols=limit_symbols) if lightweight else []

        if lightweight and target_symbols:
            df1_work = limit_closed_day_df(df1, target_symbols)
            df3_work = limit_closed_day_df(df3, target_symbols)
            df5_work = limit_closed_day_df(df5, target_symbols)
        else:
            df1_work, df3_work, df5_work = df1, df3, df5

        rebuilt = _rebuild_closed_day_all(
            df_1m_raw=df1_work,
            df_3m_raw=df3_work,
            df_5m_raw=df5_work,
            lightweight=lightweight,
            limit_symbols=limit_symbols,
        )

        out1 = keep_existing_when_empty(rebuilt.get("1m", df1_work), df1, 1, "after_recalc")
        out3 = keep_existing_when_empty(rebuilt.get("3m", df3_work), df3, 3, "after_recalc")
        out5 = keep_existing_when_empty(rebuilt.get("5m", df5_work), df5, 5, "after_recalc")

        for tf, df in ((1, out1), (3, out3), (5, out5)):
            try:
                if df is not None and not getattr(df, "empty", True):
                    set_push_merged_summary_safe(tf, df)
            except Exception:
                logger.exception("[CLOSED DAY] set merged summary failed tf=%s", tf)

        final_map = {}

        for tf, df, existing_df in ((1, out1, df1), (3, out3, df3), (5, out5, df5)):
            if df is None or df.empty:
                final_map[tf] = keep_existing_when_empty(df, existing_df, tf, "before_post_process")
                continue

            try:
                z = post_process_summary(df)
                z = keep_existing_when_empty(z, df, tf, "after_post_process")
                z = keep_existing_when_empty(z, existing_df, tf, "after_post_process_vs_existing")
                final_map[tf] = z

                if z is not None and not getattr(z, "empty", True):
                    set_push_merged_summary_safe(tf, z)
            except Exception:
                logger.exception("[CLOSED DAY] post_process failed tf=%s -> use recalc/existing output", tf)
                final_map[tf] = keep_existing_when_empty(df, existing_df, tf, "post_process_exception")

        return final_map

    except Exception:
        logger.exception("[CLOSED DAY] closed_day_recalc failed -> fallback path")
        return {
            1: rebuild_closed_day_summary_for_display_fallback(df1, 1) if df1 is not None and not df1.empty else df1,
            3: rebuild_closed_day_summary_for_display_fallback(df3, 3) if df3 is not None and not df3.empty else df3,
            5: rebuild_closed_day_summary_for_display_fallback(df5, 5) if df5 is not None and not df5.empty else df5,
        }


def rebuild_closed_day_all_if_available():
    return rebuild_closed_day_summaries_all(lightweight=True, limit_symbols=300)


__all__ = [
    "rebuild_closed_day_summary_for_display_fallback",
    "maybe_prepare_closed_day_display_cache",
    "rebuild_closed_day_summaries_all",
    "rebuild_closed_day_all_if_available",
]