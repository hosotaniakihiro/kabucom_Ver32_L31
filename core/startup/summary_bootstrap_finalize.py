# ============================================================
# File   : core/startup/summary_bootstrap_finalize.py
# Ver    : PRODUCTION-STABLE-REV13.1-SUMMARY-BOOTSTRAP-FINALIZE
# ------------------------------------------------------------
# 【概要】
#   summary bootstrap 用 finalize 群
#
# 【主な機能】
#   - recent / multi / merged の compose
#   - finalize 後の indicator / scoring 適用
#   - merged summary 保存
#   - global_data への反映
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from global_state import global_data
from trading.summary.recovery.helpers import drop_rows_outside_allowed_dates

from .summary_bootstrap_helpers import (
    safe_symbol_nunique,
    log_boot_df,
    normalize_summary_frame,
    ensure_summary_display_columns,
    final_profile_log,
    persist_summary_df_to_db,
    backfill_symbolname,
)
from .summary_bootstrap_preload import (
    get_recent_preload_df,
    early_process_preload_df,
    multi_day_coverage_ok,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVALS = (1, 3, 5)


def get_multi_summary_df(tf: int) -> pd.DataFrame:
    try:
        getter = getattr(global_data, "get_multi_summary", None)
        if callable(getter):
            try:
                df = getter(tf, source="push")
            except TypeError:
                df = getter(tf)
            if isinstance(df, pd.DataFrame):
                return df.copy(deep=True)
    except Exception:
        logger.debug("get_multi_summary failed tf=%s", tf, exc_info=True)

    try:
        if hasattr(global_data, "get_push_multi_summary"):
            df = global_data.get_push_multi_summary(tf)
            if isinstance(df, pd.DataFrame):
                return df.copy(deep=True)
    except Exception:
        logger.debug("get_push_multi_summary failed tf=%s", tf, exc_info=True)

    try:
        attr_name = f"multi_summary_{tf}"
        df = getattr(global_data, attr_name, None)
        if isinstance(df, pd.DataFrame):
            logger.warning("⚠ legacy multi summary attr fallback used tf=%s", tf)
            return df.copy(deep=True)
    except Exception:
        logger.debug("multi summary attr get failed tf=%s", tf, exc_info=True)

    return pd.DataFrame()


def compose_summary_frames(frames: list[pd.DataFrame], tf: int) -> pd.DataFrame:
    valid = []
    for i, f in enumerate(frames):
        if isinstance(f, pd.DataFrame) and not f.empty:
            logger.info("[BOOT REF][%smin/input_%d] id=%s rows=%d symbols=%d", tf, i, hex(id(f)), len(f), safe_symbol_nunique(f))
            x = normalize_summary_frame(f.copy(deep=True), tf=tf)
            x = backfill_symbolname(x)
            log_boot_df(f"compose_input_{i}", tf, x)
            valid.append(x)

    if not valid:
        return pd.DataFrame()

    if len(valid) == 1:
        return valid[0].copy(deep=True)

    try:
        combined = pd.concat(valid, ignore_index=True, sort=False)
        combined = normalize_summary_frame(combined, tf=tf)
        combined = backfill_symbolname(combined)

        if {"symbol", "datetime"}.issubset(combined.columns):
            combined = (
                combined.dropna(subset=["symbol", "datetime"])
                .sort_values(["symbol", "datetime"])
                .drop_duplicates(["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )

        log_boot_df("compose_combined", tf, combined)
        return combined
    except Exception:
        logger.exception("compose summary frames failed tf=%s", tf)
        return valid[-1].copy(deep=True)


def get_finalize_base_df(tf: int) -> pd.DataFrame:
    try:
        recent_df = _get_recent_preload_df(tf)

        try:
            merged_df = global_data.get_merged_summary(tf, source="push")
        except TypeError:
            merged_df = global_data.get_merged_summary(tf)

        multi_df = _get_multi_summary_df(tf)

        # multi-day が sparse なら recent/merged を優先
        if isinstance(multi_df, pd.DataFrame) and not multi_df.empty and int(tf) in (3, 5):
            baseline_symbols = max(_safe_symbol_nunique(recent_df), _safe_symbol_nunique(merged_df))
            if baseline_symbols > 0 and not _multi_day_coverage_ok(tf, multi_df, baseline_symbols):
                logger.warning(
                    "⚠ [%smin] sparse multi summary ignored in finalize symbols=%d baseline=%d",
                    tf,
                    _safe_symbol_nunique(multi_df),
                    baseline_symbols,
                )
                multi_df = pd.DataFrame()

        frames = [recent_df, merged_df, multi_df]
        base_df = _compose_summary_frames(frames, tf)
        return base_df

    except Exception:
        logger.exception("❌ get finalize base df failed tf=%s", tf)
        return pd.DataFrame()


def drop_price_empty_symbols(df: pd.DataFrame, tf: int) -> pd.DataFrame:
    out = normalize_summary_frame(df, tf=tf)
    if out.empty:
        return out

    try:
        close_s = None
        for c in ("close", "close_price", "price", "current_price", "CurrentPrice", "last_price", "LastPrice"):
            if c in out.columns:
                close_s = pd.to_numeric(out[c], errors="coerce")
                if close_s.notna().any():
                    break

        if close_s is None:
            return out

        valid = close_s.notna() & (close_s > 0)
        before = len(out)
        out = out.loc[valid.fillna(False)].copy().reset_index(drop=True)
        dropped = before - len(out)
        if dropped > 0:
            logger.warning("⚠ [%smin] price-empty rows removed=%d before=%d after=%d", tf, dropped, before, len(out))
        return out
    except Exception:
        logger.exception("drop price empty symbols failed tf=%s", tf)
        return out


def finalize_merged_summaries() -> None:
    for tf in DEFAULT_INTERVALS:
        try:
            base_df = get_finalize_base_df(tf)
            log_boot_df("finalize_base", tf, base_df)
            if not isinstance(base_df, pd.DataFrame) or base_df.empty:
                logger.warning("⚠ [%smin] merged summary finalize skipped: base empty", tf)
                continue

            df_after_price = drop_price_empty_symbols(base_df, tf)
            if not isinstance(df_after_price, pd.DataFrame) or df_after_price.empty:
                logger.warning("⚠ [%smin] merged summary empty after price-empty drop", tf)
                continue

            df = early_process_preload_df(df_after_price, tf, "finalize")
            df = drop_rows_outside_allowed_dates(
                df,
                label="finalize_post_process",
                include_previous_business_day=True,
                interval=int(tf),
            )
            if not isinstance(df, pd.DataFrame) or df.empty:
                logger.warning("⚠ [%smin] merged summary empty after finalize date guard", tf)
                continue

            if {"symbol", "datetime"}.issubset(df.columns):
                try:
                    df = (
                        df.dropna(subset=["symbol", "datetime"])
                        .sort_values(["symbol", "datetime"])
                        .drop_duplicates(["symbol", "datetime"], keep="last")
                        .reset_index(drop=True)
                    )
                except Exception:
                    logger.debug("final duplicate guard failed tf=%s", tf, exc_info=True)

            df = normalize_summary_frame(df, tf=tf)
            df = backfill_symbolname(df)
            df = ensure_summary_display_columns(df)

            if isinstance(df, pd.DataFrame) and not df.empty:
                safe_df = df.copy(deep=True)
                global_data.set_merged_summary(tf, safe_df.copy(deep=True))

                try:
                    latest_map = getattr(global_data, "latest_summary_by_interval", None)
                    if latest_map is None:
                        latest_map = {}
                        setattr(global_data, "latest_summary_by_interval", latest_map)
                    latest_map[tf] = safe_df.copy(deep=True)
                except Exception:
                    logger.debug("latest_summary_by_interval sync failed tf=%s", tf, exc_info=True)

                try:
                    if tf == 1:
                        global_data.latest_summary_1m = safe_df.copy(deep=True)
                    elif tf == 3:
                        global_data.latest_summary_3m = safe_df.copy(deep=True)
                    elif tf == 5:
                        global_data.latest_summary_5m = safe_df.copy(deep=True)
                except Exception:
                    logger.debug("latest_summary attr sync failed tf=%s", tf, exc_info=True)

                persist_summary_df_to_db(safe_df, tf, stage="finalize")
                logger.info("🧹 [%smin] merged summary finalized rows=%d", tf, len(safe_df))
                final_profile_log(safe_df, tf)
            else:
                logger.warning("⚠ [%smin] finalized merged summary empty", tf)

        except Exception:
            logger.exception("merged summary final filter failed tf=%s", tf)


__all__ = [
    "get_multi_summary_df",
    "compose_summary_frames",
    "get_finalize_base_df",
    "drop_price_empty_symbols",
    "finalize_merged_summaries",
]