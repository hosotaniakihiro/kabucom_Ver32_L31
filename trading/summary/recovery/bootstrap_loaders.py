# ============================================================
# File   : trading/summary/recovery/bootstrap_loaders.py
# Ver    : PRODUCTION-STABLE-REV1.0-BOOTSTRAP-LOADERS
# ------------------------------------------------------------
# ✔ split loaders preferred / compat fallback
# ✔ load_last_summary_datetime 互換吸収
# ✔ symbol normalize
# ✔ source 1m tail load
# ✔ recent 1m history load for recalc
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from trading.summary.recovery.helpers import normalize_datetime_columns
from .bootstrap_logging import log_source_date_breakdown
from .loaders_push import (
    filter_push_after,
    load_push_df_for_dates,
    load_runtime_push_df,
    load_runtime_push_delta_df,
    normalize_symbols,
)

logger = logging.getLogger(__name__)

# ============================================================
# split loaders preferred / compat fallback
# ============================================================
try:
    from trading.summary.recovery.loaders_push import (
        filter_push_after,
        load_push_df_for_dates,
        load_runtime_push_df,
    )
    from trading.summary.recovery.loaders_summary import (
        load_last_summary_datetime,
        load_recent_summary_tail_per_symbol,
    )
    from trading.summary.recovery.loaders_common import (
        normalize_symbols as _normalize_symbols_common,
    )
except Exception:
    from trading.summary.recovery.loaders import (  # type: ignore
        filter_push_after,
        load_last_summary_datetime,
        load_push_df_for_dates,
        load_recent_summary_tail_per_symbol,
        load_runtime_push_df,
    )

    def _normalize_symbols_common(values):
        out = []
        try:
            for v in values or []:
                s = str(v).strip()
                if not s or s.lower() in {"nan", "none", "nat"}:
                    continue
                if "." in s:
                    s = s.split(".", 1)[0].strip()
                if s not in out:
                    out.append(s)
        except Exception:
            pass
        return out

from .preload import load_recent_history_for_cache


def load_last_summary_datetime_compat(
    interval: int,
    *,
    target_dates_ctx=None,
    anchor_day=None,
    max_allowed_dt=None,
):
    attempts = [
        lambda: load_last_summary_datetime(
            interval,
            target_dates=target_dates_ctx,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        ),
        lambda: load_last_summary_datetime(
            interval,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        ),
        lambda: load_last_summary_datetime(
            interval,
            anchor_day=anchor_day,
        ),
        lambda: load_last_summary_datetime(
            interval,
            max_allowed_dt=max_allowed_dt,
        ),
        lambda: load_last_summary_datetime(interval),
    ]

    last_exc = None

    for idx, fn in enumerate(attempts, start=1):
        try:
            value = fn()
            if idx > 1:
                logger.warning(
                    "[summary_recovery] load_last_summary_datetime compat fallback used "
                    "interval=%s attempt=%s anchor_day=%s max_allowed_dt=%s",
                    interval,
                    idx,
                    anchor_day,
                    max_allowed_dt,
                )
            return value
        except TypeError as e:
            last_exc = e
            continue
        except Exception:
            raise

    if last_exc is not None:
        raise last_exc
    return None


def normalize_symbols(values: Iterable) -> list[str]:
    try:
        return _normalize_symbols_common(values)
    except Exception:
        out: list[str] = []
        try:
            for v in values:
                s = str(v).strip()
                if not s or s.lower() in {"nan", "none", "nat"}:
                    continue
                if "." in s:
                    s = s.split(".", 1)[0].strip()
                if s not in out:
                    out.append(s)
        except Exception:
            logger.exception("[summary_recovery] normalize symbols failed")
        return out


def load_src_1m_tail_for_symbols(
    *,
    symbols: list[str],
    bars_per_symbol: int,
    end_dt,
    target_dates_ctx=None,
    anchor_day=None,
    max_allowed_dt=None,
) -> pd.DataFrame:
    symbols = normalize_symbols(symbols)
    bars_per_symbol = max(int(bars_per_symbol), 1)

    if not symbols:
        logger.info(
            "[summary_recovery] src_1m tail load skipped reason=no_symbols bars_per_symbol=%s",
            bars_per_symbol,
        )
        return pd.DataFrame()

    try:
        df = load_recent_summary_tail_per_symbol(
            1,
            bars_per_symbol=bars_per_symbol,
            end_dt=end_dt,
            target_dates=target_dates_ctx,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
            symbols=symbols,
        )

        if df.empty:
            logger.info(
                "[summary_recovery] src_1m tail load empty symbols=%s bars_per_symbol=%s end_dt=%s target_dates=%s",
                len(symbols),
                bars_per_symbol,
                end_dt,
                [] if not target_dates_ctx else [str(x) for x in target_dates_ctx],
            )
            return pd.DataFrame()

        log_source_date_breakdown(
            df,
            label="src_1m_tail_raw",
            target_dates_ctx=target_dates_ctx,
            anchor_day=anchor_day,
            required_bars_per_symbol=bars_per_symbol,
        )
        return normalize_datetime_columns(df.reset_index(drop=True), interval=1)

    except Exception:
        logger.exception(
            "[summary_recovery] src_1m tail load failed symbols=%s bars_per_symbol=%s",
            0 if symbols is None else len(symbols),
            bars_per_symbol,
        )
        return pd.DataFrame()


def load_recent_1m_history_for_recalc(
    last_1m_dt,
    *,
    dates,
    anchor_day,
    max_allowed_dt,
    min_bars: int,
) -> pd.DataFrame:
    try:
        hist_raw = load_recent_history_for_cache(
            1,
            last_1m_dt,
            min_bars=min_bars,
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )
        hist_raw = normalize_datetime_columns(hist_raw, interval=1)

        log_source_date_breakdown(
            hist_raw,
            label="recent_1m_hist_for_recalc",
            target_dates_ctx=dates,
            anchor_day=anchor_day,
            required_bars_per_symbol=min_bars,
        )
        return hist_raw
    except Exception:
        logger.exception("[summary_recovery] load recent 1m history for recalc failed")
        return pd.DataFrame()


__all__ = [
    "filter_push_after",
    "load_push_df_for_dates",
    "load_runtime_push_df",
    "load_runtime_push_delta_df",
    "normalize_symbols",
    "load_last_summary_datetime_compat",
    "load_recent_1m_history_for_recalc",
    "load_src_1m_tail_for_symbols",
]