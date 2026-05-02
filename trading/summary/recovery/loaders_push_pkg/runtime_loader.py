# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/runtime_loader.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-RUNTIME-LOADER
# ------------------------------------------------------------
# 【概要】
#   runtime PUSH dataframe loader
#
# 【主な機能】
#   ✔ global_data.push_df 読込
#   ✔ global_data.push_dataframe 読込
#   ✔ global_data.stream_df 読込
#   ✔ global_data.tick_df 読込
#   ✔ global_data.push_rows_df 読込
#   ✔ push_rows / tick_rows / push_buffer_rows 読込
#   ✔ DB fallback
#   ✔ checkpoint 以降の delta 読込
#   ✔ warmup_minutes 対応
#   ✔ symbol 絞り込み対応
#
# 【重要】
#   - runtime が空の場合のみ DB fallback
#   - deltaでは last_dt - warmup_minutes 以降を読む
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Optional

import pandas as pd

from global_state import global_data

from trading.summary.recovery.helpers import safe_get_series
from trading.summary.recovery.loaders_common import (
    now_naive,
    sanitize_checkpoint_dt,
)

from .db_loader import load_push_df_for_dates
from .filters import (
    filter_future_ticks,
    filter_market_session_ticks,
)
from .normalizer import (
    normalize_push_df,
    normalize_symbols,
)
from .timezone import (
    to_tz_naive_datetime_series,
    to_tz_naive_timestamp,
)

logger = logging.getLogger(__name__)


def load_runtime_push_df(
    *,
    now: Optional[dt.datetime] = None,
    allow_db_fallback: bool = True,
    drop_future_ticks: bool = True,
    market_hours_only: bool = False,
) -> pd.DataFrame:
    if now is None:
        now = dt.datetime.now()

    now_safe = to_tz_naive_timestamp(now, label="load_runtime_push_df.now")
    if now_safe is None:
        now_safe = to_tz_naive_timestamp(now_naive(), label="load_runtime_push_df.now_fallback")

    candidates = [
        getattr(global_data, "push_df", None),
        getattr(global_data, "push_dataframe", None),
        getattr(global_data, "stream_df", None),
        getattr(global_data, "tick_df", None),
        getattr(global_data, "push_rows_df", None),
    ]

    for c in candidates:
        if isinstance(c, pd.DataFrame) and not c.empty:
            try:
                out = normalize_push_df(c)

                if drop_future_ticks and not out.empty:
                    out = filter_future_ticks(
                        out,
                        datetime_col="tick_time",
                        now_dt=now_safe,
                        tolerance_minutes=2,
                        label="runtime_push_df",
                    )

                if market_hours_only and not out.empty:
                    out = filter_market_session_ticks(
                        out,
                        datetime_col="tick_time",
                        label="runtime_push_df",
                    )

                if not out.empty:
                    logger.info(
                        "[summary.recovery.loaders_push.runtime_loader] runtime push loaded from dataframe rows=%d symbols=%d latest_tick=%s",
                        len(out),
                        int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
                        out["tick_time"].max()
                        if "tick_time" in out.columns and not out.empty
                        else None,
                    )
                    return out

            except Exception:
                logger.exception(
                    "[summary.recovery.loaders_push.runtime_loader] runtime push normalize failed"
                )

    for attr in ("push_rows", "tick_rows", "push_buffer_rows"):
        try:
            rows = getattr(global_data, attr, None)
            if rows:
                df = normalize_push_df(pd.DataFrame(rows))

                if drop_future_ticks and not df.empty:
                    df = filter_future_ticks(
                        df,
                        datetime_col="tick_time",
                        now_dt=now_safe,
                        tolerance_minutes=2,
                        label=f"runtime_{attr}",
                    )

                if market_hours_only and not df.empty:
                    df = filter_market_session_ticks(
                        df,
                        datetime_col="tick_time",
                        label=f"runtime_{attr}",
                    )

                if not df.empty:
                    logger.info(
                        "[summary.recovery.loaders_push.runtime_loader] runtime push loaded from %s rows=%d symbols=%d latest_tick=%s",
                        attr,
                        len(df),
                        int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
                        df["tick_time"].max()
                        if "tick_time" in df.columns and not df.empty
                        else None,
                    )
                    return df

        except Exception:
            logger.debug(
                "[summary.recovery.loaders_push.runtime_loader] runtime push rows load failed attr=%s",
                attr,
                exc_info=True,
            )

    if allow_db_fallback:
        try:
            today = now_safe.date() if now_safe is not None else dt.datetime.now().date()
            db_df = load_push_df_for_dates(
                [today],
                now=now_safe,
                drop_future_ticks=drop_future_ticks,
                market_hours_only=market_hours_only,
            )
            db_df = normalize_push_df(db_df)
            if not db_df.empty:
                latest_tick = (
                    db_df["tick_time"].max()
                    if "tick_time" in db_df.columns and not db_df.empty
                    else None
                )
                logger.warning(
                    "[summary.recovery.loaders_push.runtime_loader] runtime push empty/stale -> DB fallback rows=%d symbols=%d latest_tick=%s",
                    len(db_df),
                    int(db_df["symbol"].nunique()) if "symbol" in db_df.columns else 0,
                    latest_tick,
                )
                return db_df

        except Exception:
            logger.exception(
                "[summary.recovery.loaders_push.runtime_loader] DB fallback push load failed"
            )

    return pd.DataFrame()


def load_runtime_push_delta_df(
    *,
    last_dt: Optional[pd.Timestamp],
    now: Optional[dt.datetime] = None,
    allow_db_fallback: bool = True,
    drop_future_ticks: bool = True,
    market_hours_only: bool = False,
    warmup_minutes: int = 10,
    symbols: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    if now is None:
        now = dt.datetime.now()

    now_safe = to_tz_naive_timestamp(now, label="load_runtime_push_delta_df.now")
    if now_safe is None:
        now_safe = to_tz_naive_timestamp(now_naive(), label="load_runtime_push_delta_df.now_fallback")

    last_dt = sanitize_checkpoint_dt(
        last_dt,
        label="load_runtime_push_delta_df.last_dt",
        interval=None,
    )
    last_dt_safe = to_tz_naive_timestamp(
        last_dt,
        label="load_runtime_push_delta_df.last_dt_sanitized",
    )

    start_dt = None
    if last_dt_safe is not None and not pd.isna(last_dt_safe):
        try:
            start_dt = last_dt_safe - pd.Timedelta(
                minutes=max(int(warmup_minutes), 0)
            )
        except Exception:
            start_dt = last_dt_safe

    start_dt_safe = (
        to_tz_naive_timestamp(start_dt, label="load_runtime_push_delta_df.start_dt")
        if start_dt is not None
        else None
    )

    runtime_df = load_runtime_push_df(
        now=now_safe,
        allow_db_fallback=False,
        drop_future_ticks=drop_future_ticks,
        market_hours_only=market_hours_only,
    )

    symbol_list = normalize_symbols(symbols)

    if isinstance(runtime_df, pd.DataFrame) and not runtime_df.empty:
        out = runtime_df.copy()

        if start_dt_safe is not None and "tick_time" in out.columns:
            tick_s = to_tz_naive_datetime_series(
                safe_get_series(out, "tick_time"),
                label="load_runtime_push_delta_df.tick_time",
            )

            out = out.loc[tick_s.notna()].copy()
            tick_s = tick_s.loc[out.index]

            try:
                out["tick_time"] = tick_s
            except Exception:
                pass

            out = out.loc[tick_s >= start_dt_safe].copy()

        if symbol_list and "symbol" in out.columns:
            out = out.loc[out["symbol"].astype(str).isin(symbol_list)].copy()

        out = out.reset_index(drop=True)

        logger.info(
            "[summary.recovery.loaders_push.runtime_loader] runtime delta loaded rows=%d symbols=%d start_dt=%s last_dt=%s requested_symbols=%d tick_min=%s tick_max=%s",
            len(out),
            int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
            start_dt_safe,
            last_dt_safe,
            len(symbol_list),
            out["tick_time"].min() if not out.empty and "tick_time" in out.columns else None,
            out["tick_time"].max() if not out.empty and "tick_time" in out.columns else None,
        )

        return out

    if allow_db_fallback:
        try:
            today = now_safe.date() if now_safe is not None else dt.datetime.now().date()
            db_df = load_push_df_for_dates(
                [today],
                now=now_safe,
                drop_future_ticks=drop_future_ticks,
                market_hours_only=market_hours_only,
                start_dt=start_dt_safe,
                end_dt=None,
                symbols=symbol_list if symbol_list else None,
            )

            if not db_df.empty:
                logger.info(
                    "[summary.recovery.loaders_push.runtime_loader] DB delta fallback rows=%d symbols=%d start_dt=%s last_dt=%s requested_symbols=%d",
                    len(db_df),
                    int(db_df["symbol"].nunique()) if "symbol" in db_df.columns else 0,
                    start_dt_safe,
                    last_dt_safe,
                    len(symbol_list),
                )
                return db_df

        except Exception:
            logger.exception(
                "[summary.recovery.loaders_push.runtime_loader] load_runtime_push_delta_df DB fallback failed"
            )

    return pd.DataFrame()


__all__ = [
    "load_runtime_push_df",
    "load_runtime_push_delta_df",
]