# ============================================================
# File   : trading/summary/recovery/cache_seed.py
# Ver    : PRODUCTION-STABLE-REV1.1-CACHE-SEED-RAW-SEPARATION
# ------------------------------------------------------------
# ✔ higher TF delta rows limiting
# ✔ cache seed build
# ✔ raw / upsert 用 DF を分離
# ✔ cache seed は raw のまま返す
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.summary.recovery.helpers import (
    merge_summary_frames_with_priority,
    normalize_datetime_columns,
)
from .preload import load_recent_history_for_cache

logger = logging.getLogger(__name__)


def limit_recent_tf_rows(
    df: pd.DataFrame,
    interval: int,
    *,
    keep_bars_per_symbol: int | None = None,
    keep_bars: int | None = None,
) -> pd.DataFrame:
    """
    higher TF delta を各symbolの直近数本に制限する。
    keep_bars / keep_bars_per_symbol の両方を受ける後方互換版。
    """
    out = normalize_datetime_columns(df, interval=int(interval))
    if out.empty:
        return out

    if "symbol" not in out.columns or "datetime" not in out.columns:
        return out

    try:
        limit = keep_bars_per_symbol
        if limit is None:
            limit = keep_bars
        if limit is None:
            logger.info(
                "[summary_recovery] limit recent tf rows skipped interval=%s reason=no_limit",
                interval,
            )
            return out

        keep_n = max(int(limit), 1)
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = (
            out.sort_values(["symbol", "datetime"])
            .groupby("symbol", group_keys=False)
            .tail(keep_n)
            .reset_index(drop=True)
        )
        logger.info(
            "[summary_recovery] limited recent tf rows interval=%s keep_bars_per_symbol=%s rows=%d symbols=%d",
            interval,
            keep_n,
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns and not out.empty else 0,
        )
        return out
    except Exception:
        logger.exception(
            "[summary_recovery] limit recent tf rows failed interval=%s keep_bars_per_symbol=%s keep_bars=%s",
            interval,
            keep_bars_per_symbol,
            keep_bars,
        )
        return out

def build_cache_seed_with_recent_history(
    interval: int,
    delta_df: pd.DataFrame,
    last_dt,
    *,
    min_bars: int,
    target_dates_ctx=None,
    anchor_day=None,
    max_allowed_dt=None,
) -> pd.DataFrame:
    """
    cache 更新用:
      persisted recent history + delta を merge し、
      各symbolの直近履歴を保持した状態で cache へ渡す。

    重要:
      - ここでは raw seed を返す
      - finalize_for_upsert は呼ばない
    """
    try:
        hist_raw = load_recent_history_for_cache(
            int(interval),
            last_dt,
            min_bars=max(int(min_bars), 1),
            target_dates_ctx=target_dates_ctx,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
            fallback_snapshot=True,
        )
        hist_raw = normalize_datetime_columns(hist_raw, interval=int(interval))

        delta_raw = normalize_datetime_columns(delta_df, interval=int(interval))

        merged_raw = merge_summary_frames_with_priority(hist_raw, delta_raw, interval=int(interval))
        merged_raw = normalize_datetime_columns(merged_raw, interval=int(interval))

        if merged_raw.empty:
            logger.info(
                "[summary_recovery] cache seed empty interval=%s hist_rows=%d delta_rows=%d",
                interval,
                len(hist_raw) if isinstance(hist_raw, pd.DataFrame) else 0,
                len(delta_raw) if isinstance(delta_raw, pd.DataFrame) else 0,
            )
            return merged_raw

        if "symbol" in merged_raw.columns and "datetime" in merged_raw.columns:
            merged_raw["datetime"] = pd.to_datetime(merged_raw["datetime"], errors="coerce")
            merged_raw = (
                merged_raw.dropna(subset=["symbol", "datetime"])
                .sort_values(["symbol", "datetime"])
                .groupby("symbol", group_keys=False)
                .tail(max(int(min_bars), 1))
                .reset_index(drop=True)
            )

        logger.info(
            "[summary_recovery] cache seed built interval=%s rows=%d symbols=%d min_bars=%d",
            interval,
            len(merged_raw),
            int(merged_raw["symbol"].nunique()) if "symbol" in merged_raw.columns and not merged_raw.empty else 0,
            max(int(min_bars), 1),
        )
        return merged_raw

    except Exception:
        logger.exception(
            "[summary_recovery] build cache seed failed interval=%s min_bars=%s",
            interval,
            min_bars,
        )
        return pd.DataFrame()