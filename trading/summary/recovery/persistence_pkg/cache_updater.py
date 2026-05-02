# ============================================================
# File   : trading/summary/recovery/persistence_pkg/cache_updater.py
# Ver    : PRODUCTION-STABLE-REV9.1-CACHE-UPDATER
#          -MERGE-SUMMARY-HISTORY-BEFORE-CACHE-BUILD
#          -PREVENT-TECHNICAL-CACHE-DROP
# ------------------------------------------------------------
# 【概要】
#   global_data merged summary cache updater
#
# 【REV9.1 修正】
#   ✔ cache update 時に summary_history_cache を取得
#   ✔ raw latest rows と history を結合してから completed-ish cache を作る
#   ✔ realtime_engine / recovery persistence が latest rows だけで cache を上書きし、
#      rsi/macd/slope/mtf が消える問題を軽減
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .imports import global_data
from .column_utils import safe_df, coalesce_duplicate_columns, pick_numeric_series_nan
from .datetime_utils import normalize_datetime_like, parse_datetime_series_safely
from .db_normalizer import normalize_numeric_like
from .score_utils import ensure_score_columns
from .cache_builder import make_completedish_cache_df

logger = logging.getLogger(__name__)


def _count_nonnull_numeric(df: pd.DataFrame, col: str) -> int:
    try:
        if df is None or df.empty or col not in df.columns:
            return 0
        return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        return 0


def _safe_symbols(df: pd.DataFrame) -> int:
    try:
        if df is None or df.empty or "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).str.strip().replace("", pd.NA).dropna().nunique())
    except Exception:
        return 0


def _get_summary_history(interval: int) -> pd.DataFrame:
    """
    global_data から summary history を取得する。
    取得できない場合は empty。
    """
    try:
        if global_data is None:
            return pd.DataFrame()

        candidates = [
            "get_summary_history",
            "get_summary_history_df",
            "get_history_summary",
        ]

        for name in candidates:
            fn = getattr(global_data, name, None)
            if callable(fn):
                try:
                    df = fn(int(interval), source="push")
                except TypeError:
                    try:
                        df = fn(int(interval))
                    except TypeError:
                        df = fn(interval, "push")
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df

        for attr in [
            "summary_history",
            "summary_history_cache",
            "push_summary_history",
            "_summary_history",
            "_summary_history_cache",
        ]:
            obj = getattr(global_data, attr, None)
            if isinstance(obj, dict):
                for key in [
                    int(interval),
                    str(int(interval)),
                    f"{int(interval)}min",
                    ("push", int(interval)),
                    (int(interval), "push"),
                ]:
                    df = obj.get(key)
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        return df

    except Exception:
        logger.debug(
            "[summary.recovery.persistence] get summary history failed interval=%s",
            interval,
            exc_info=True,
        )

    return pd.DataFrame()


def _normalize_for_cache_merge(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        out = coalesce_duplicate_columns(out)
        out = normalize_datetime_like(out)
        out = normalize_numeric_like(out)
        out = ensure_score_columns(out)

        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip()
            out = out[out["symbol"].ne("")].copy()

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.dropna(subset=["datetime"]).copy()

        return out

    except Exception:
        logger.debug("[summary.recovery.persistence] normalize for cache merge failed", exc_info=True)
        return safe_df(df)


def _merge_raw_with_history(raw: pd.DataFrame, history: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    raw = _normalize_for_cache_merge(raw)
    history = _normalize_for_cache_merge(history)

    if raw.empty:
        return history

    if history.empty:
        return raw

    try:
        merged = pd.concat([history, raw], ignore_index=True, sort=False)
        merged = _normalize_for_cache_merge(merged)

        if {"symbol", "datetime"}.issubset(merged.columns):
            merged = (
                merged.sort_values(["symbol", "datetime"], kind="stable")
                .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )

        logger.info(
            "[summary.recovery.persistence] cache merge raw+history interval=%s raw_rows=%s history_rows=%s merged_rows=%s symbols=%s "
            "rsi=%s macd=%s mtf=%s score_mtf=%s",
            interval,
            len(raw),
            len(history),
            len(merged),
            _safe_symbols(merged),
            _count_nonnull_numeric(merged, "rsi"),
            _count_nonnull_numeric(merged, "macd"),
            _count_nonnull_numeric(merged, "mtf"),
            _count_nonnull_numeric(merged, "score_mtf"),
        )

        return merged

    except Exception:
        logger.debug(
            "[summary.recovery.persistence] raw/history cache merge failed interval=%s",
            interval,
            exc_info=True,
        )
        return raw


def update_global_cache(df: pd.DataFrame, interval: int) -> None:
    raw = safe_df(df)
    if raw.empty:
        logger.info("[summary.recovery.persistence] cache update skipped empty interval=%s", interval)
        return

    try:
        raw = _normalize_for_cache_merge(raw)

        latest_dt = None
        if "datetime" in raw.columns:
            dt_s = parse_datetime_series_safely(
                raw["datetime"],
                base_df=raw,
                col_name="datetime",
                allow_time_only=True,
            )
            if dt_s.notna().any():
                latest_dt = dt_s.max()

        logger.info(
            "[summary.recovery.persistence] cache raw input interval=%s rows=%s symbols=%s latest_dt=%s "
            "score_nonnull=%s buy_nonnull=%s sell_nonnull=%s rsi=%s macd=%s mtf=%s score_mtf=%s",
            interval,
            len(raw),
            raw["symbol"].astype(str).nunique() if "symbol" in raw.columns else 0,
            latest_dt,
            int(pick_numeric_series_nan(raw, ["score", "score_total", "display_score", "final_score"]).notna().sum()),
            int(pick_numeric_series_nan(raw, ["score_buy", "buy_score", "buy"]).notna().sum()),
            int(pick_numeric_series_nan(raw, ["score_sell", "sell_score", "sell"]).notna().sum()),
            _count_nonnull_numeric(raw, "rsi"),
            _count_nonnull_numeric(raw, "macd"),
            _count_nonnull_numeric(raw, "mtf"),
            _count_nonnull_numeric(raw, "score_mtf"),
        )

        history = _get_summary_history(int(interval))
        cache_source = _merge_raw_with_history(raw, history, interval=int(interval))

        cache_df = make_completedish_cache_df(cache_source, interval=int(interval))

        if cache_df.empty:
            logger.warning(
                "[summary.recovery.persistence] cache update skipped interval=%s reason=no_completedish_rows raw_rows=%s history_rows=%s",
                interval,
                len(raw),
                len(history) if isinstance(history, pd.DataFrame) else 0,
            )
            return

        if global_data is not None and hasattr(global_data, "set_merged_summary"):
            global_data.set_merged_summary(int(interval), cache_df, source="push")
        elif global_data is not None and hasattr(global_data, "set_push_merged_summary"):
            global_data.set_push_merged_summary(int(interval), cache_df)
        else:
            logger.warning(
                "[summary.recovery.persistence] global_data set_merged_summary not available interval=%s",
                interval,
            )
            return

        logger.info(
            "[summary.recovery.persistence] cache updated interval=%s raw_rows=%s history_rows=%s cache_rows=%s symbols=%s latest_dt=%s "
            "score_nonnull=%s buy_nonnull=%s sell_nonnull=%s rsi=%s macd=%s signal=%s mtf=%s score_mtf=%s",
            interval,
            len(raw),
            len(history) if isinstance(history, pd.DataFrame) else 0,
            len(cache_df),
            cache_df["symbol"].nunique() if "symbol" in cache_df.columns else 0,
            latest_dt,
            int(pick_numeric_series_nan(cache_df, ["score", "score_total", "display_score", "final_score"]).notna().sum()),
            int(pick_numeric_series_nan(cache_df, ["score_buy", "buy_score", "buy"]).notna().sum()),
            int(pick_numeric_series_nan(cache_df, ["score_sell", "sell_score", "sell"]).notna().sum()),
            _count_nonnull_numeric(cache_df, "rsi"),
            _count_nonnull_numeric(cache_df, "macd"),
            _count_nonnull_numeric(cache_df, "signal"),
            _count_nonnull_numeric(cache_df, "mtf"),
            _count_nonnull_numeric(cache_df, "score_mtf"),
        )

    except Exception:
        logger.exception("[summary.recovery.persistence] cache update failed interval=%s", interval)


__all__ = [
    "update_global_cache",
]