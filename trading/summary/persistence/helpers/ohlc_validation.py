# ============================================================
# File   : trading/summary/persistence/helpers/ohlc_validation.py
# Version: Ver1.0-SUMMARY-OHLC-VALIDATION
# ------------------------------------------------------------
# OHLC 検証 / 1分足緩和判定
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .dataframe_utils import _ensure_dataframe, _safe_get_series

logger = logging.getLogger(__name__)


def _pick_price_series(df: pd.DataFrame, candidates: list[str]) -> pd.Series:
    idx = df.index
    base = pd.Series(np.nan, index=idx, dtype="float64")
    for c in candidates:
        if c in df.columns:
            try:
                s = pd.to_numeric(_safe_get_series(df, c), errors="coerce").replace([np.inf, -np.inf], np.nan)
                base = base.combine_first(s)
            except Exception:
                logger.debug("[SUMMARY] pick price failed col=%s", c, exc_info=True)
    return base


def _drop_invalid_ohlc_rows(df: pd.DataFrame, interval: int, stage: str) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    open_s = _pick_price_series(out, ["open", "open_price"]).mask(lambda s: s <= 0, np.nan)
    high_s = _pick_price_series(out, ["high", "high_price"]).mask(lambda s: s <= 0, np.nan)
    low_s = _pick_price_series(out, ["low", "low_price"]).mask(lambda s: s <= 0, np.nan)
    close_s = _pick_price_series(
        out,
        ["close", "close_price", "price", "current_price", "CurrentPrice", "last_price"]
    ).mask(lambda s: s <= 0, np.nan)

    if int(interval) == 1:
        open_s = open_s.combine_first(close_s)
        high_s = high_s.combine_first(close_s)
        low_s = low_s.combine_first(close_s)

        symbol_s = _safe_get_series(out, "symbol")
        datetime_s = _safe_get_series(out, "datetime")

        symbol_ok = symbol_s.notna() if symbol_s is not None else pd.Series(False, index=out.index)
        datetime_ok = pd.to_datetime(datetime_s, errors="coerce").notna() if datetime_s is not None else pd.Series(False, index=out.index)
        close_ok = close_s.notna()

        valid = symbol_ok & datetime_ok & close_ok

        out["open"] = open_s
        out["high"] = high_s
        out["low"] = low_s
        out["close"] = close_s

        try:
            out["open_price"] = open_s
            out["high_price"] = high_s
            out["low_price"] = low_s
            out["close_price"] = close_s
        except Exception:
            logger.debug("[SUMMARY] 1min alias sync failed", exc_info=True)

        before = len(out)
        out = out.loc[valid].copy()

        if before != len(out):
            logger.warning(
                "[SUMMARY] invalid OHLC removed(relaxed-1min) stage=%s interval=%s before=%d after=%d",
                stage,
                interval,
                before,
                len(out),
            )

        return out

    valid = (
        open_s.notna()
        & high_s.notna()
        & low_s.notna()
        & close_s.notna()
        & (high_s >= low_s)
        & (high_s >= open_s)
        & (high_s >= close_s)
        & (low_s <= open_s)
        & (low_s <= close_s)
    )

    before = len(out)
    out = out.loc[valid].copy()

    if before != len(out):
        logger.warning(
            "[SUMMARY] invalid OHLC removed stage=%s interval=%s before=%d after=%d",
            stage,
            interval,
            before,
            len(out),
        )

    return out