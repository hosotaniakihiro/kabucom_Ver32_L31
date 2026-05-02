# ============================================================
# File   : trading/summary/pipeline/trade_universe_filter.py
# Version: Ver32_L05-SPLIT-TRADE-UNIVERSE-FILTER
# Purpose:
#   summary_pipeline 用の共通価格フィルタ
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_PIPELINE_MIN_PRICE = 200.0

try:
    from trading.summary.filters import (
        apply_common_trade_universe_filter as _external_apply_common_trade_universe_filter,
    )
except Exception:
    _external_apply_common_trade_universe_filter = None


def env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def resolve_pipeline_min_price() -> float:
    v1 = os.getenv("TRADE_UNIVERSE_MIN_PRICE")
    if v1 is not None and str(v1).strip() != "":
        return env_float("TRADE_UNIVERSE_MIN_PRICE", DEFAULT_PIPELINE_MIN_PRICE)

    v2 = os.getenv("SUMMARY_PIPELINE_MIN_PRICE")
    if v2 is not None and str(v2).strip() != "":
        return env_float("SUMMARY_PIPELINE_MIN_PRICE", DEFAULT_PIPELINE_MIN_PRICE)

    return float(DEFAULT_PIPELINE_MIN_PRICE)


def select_pipeline_price_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    for c in ("close", "close_price", "price", "current_price", "last_price"):
        if c in df.columns:
            return c
    return None


def fallback_apply_common_trade_universe_filter(
    df: pd.DataFrame,
    *,
    min_price: Optional[float] = None,
    context: str = "",
    log: Optional[logging.Logger] = None,
) -> pd.DataFrame:
    lg = log or logger

    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    price_col = select_pipeline_price_col(out)
    min_price_v = float(min_price) if min_price is not None else resolve_pipeline_min_price()

    before = len(out)

    if price_col is None:
        lg.warning(
            "[summary_pipeline][TRADE-UNIVERSE] price column missing context=%s rows=%s cols=%s",
            context,
            before,
            list(out.columns),
        )
        return out

    out[price_col] = pd.to_numeric(out[price_col], errors="coerce").fillna(0.0)
    out = out[out[price_col] > min_price_v].copy()

    lg.info(
        "[summary_pipeline][TRADE-UNIVERSE] context=%s price_col=%s condition='%s > %.1f' before=%s after=%s skipped=%s",
        context,
        price_col,
        price_col,
        min_price_v,
        before,
        len(out),
        before - len(out),
    )

    return out


def apply_pipeline_common_trade_universe_filter(
    df: pd.DataFrame,
    *,
    interval: int,
    context: str,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    min_price = resolve_pipeline_min_price()

    if _external_apply_common_trade_universe_filter is not None:
        try:
            out = _external_apply_common_trade_universe_filter(
                df,
                min_price=min_price,
                context=f"{context} interval={interval}",
                log=logger,
            )
            if isinstance(out, pd.DataFrame):
                return out
        except Exception as e:
            logger.error(
                "[summary_pipeline][TRADE-UNIVERSE] external filter failed interval=%s err=%s: %s",
                interval,
                type(e).__name__,
                str(e)[:300],
                exc_info=False,
            )

    return fallback_apply_common_trade_universe_filter(
        df,
        min_price=min_price,
        context=f"{context} interval={interval}",
        log=logger,
    )


__all__ = [
    "DEFAULT_PIPELINE_MIN_PRICE",
    "resolve_pipeline_min_price",
    "apply_pipeline_common_trade_universe_filter",
]
