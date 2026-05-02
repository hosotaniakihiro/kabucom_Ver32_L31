# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/dataframe_utils.py
# Version: PRODUCTION-STABLE-REV1.0-DATAFRAME-UTILS
# ------------------------------------------------------------
# 【概要】
#   DataFrame 正規化 / OHLCV列名ゆれ吸収 / latest per symbol
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .constants import PRICE_ALIAS_MAP, VOLUME_ALIASES
from .datetime_guard import (
    normalize_higher_tf_datetime,
    drop_future_datetime_rows,
)

logger = logging.getLogger(__name__)


def ensure_df(df: Any) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            out = df.copy()
        else:
            out = pd.DataFrame(df)

        if out.empty:
            return pd.DataFrame()

        out.columns = [str(c) for c in out.columns]

        if out.columns.duplicated().any():
            dup = out.columns[out.columns.duplicated()].tolist()
            logger.warning("[MTF HISTORY BOOTSTRAP] duplicate columns removed=%s", dup)
            out = out.loc[:, ~out.columns.duplicated(keep="last")].copy()

        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[MTF HISTORY BOOTSTRAP] ensure df failed")
        return pd.DataFrame()


def coalesce_numeric(out: pd.DataFrame, dst: str, candidates: Iterable[str]) -> pd.DataFrame:
    for c in candidates:
        if c not in out.columns:
            continue

        try:
            s = pd.to_numeric(out[c], errors="coerce").replace([np.inf, -np.inf], np.nan)

            if dst not in out.columns:
                out[dst] = s
            else:
                base = pd.to_numeric(out[dst], errors="coerce").replace([np.inf, -np.inf], np.nan)
                out[dst] = base.combine_first(s)

        except Exception:
            logger.debug("[MTF HISTORY BOOTSTRAP] coalesce failed dst=%s src=%s", dst, c, exc_info=True)

    return out


def normalize_symbol_value(v: Any) -> str:
    try:
        if pd.isna(v):
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def normalize_summary_df(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_df(df)
    if out.empty:
        return out

    try:
        if "symbol" not in out.columns:
            for c in ("code", "ticker", "Symbol", "symbol_code"):
                if c in out.columns:
                    out["symbol"] = out[c]
                    break

        if "symbol" not in out.columns:
            logger.warning("[MTF HISTORY BOOTSTRAP] normalize failed: symbol missing")
            return pd.DataFrame()

        out["symbol"] = out["symbol"].map(normalize_symbol_value)
        out = out[out["symbol"].ne("")].copy()

        if out.empty:
            return out

        if "symbolname" not in out.columns:
            for c in ("name", "company_name", "SymbolName", "銘柄名"):
                if c in out.columns:
                    out["symbolname"] = out[c]
                    break

            if "symbolname" not in out.columns:
                out["symbolname"] = ""

        out["symbolname"] = out["symbolname"].fillna("").astype(str)

        if "datetime" not in out.columns:
            for c in ("start_time", "time", "end_time", "timestamp", "current_price_time"):
                if c in out.columns:
                    out["datetime"] = out[c]
                    break

        out = normalize_higher_tf_datetime(
            out,
            interval=int(out["interval"].dropna().iloc[0])
            if "interval" in out.columns and out["interval"].notna().any()
            else 1,
        )

        if "datetime" not in out.columns:
            logger.warning("[MTF HISTORY BOOTSTRAP] normalize failed: datetime missing")
            return pd.DataFrame()

        out = out.dropna(subset=["datetime"]).copy()

        for dst, candidates in PRICE_ALIAS_MAP.items():
            out = coalesce_numeric(out, dst, candidates)

        out = coalesce_numeric(out, "volume", VOLUME_ALIASES)

        for c in ("open", "high", "low", "close", "volume"):
            if c not in out.columns:
                out[c] = pd.NA
            out[c] = pd.to_numeric(out[c], errors="coerce").replace([np.inf, -np.inf], np.nan)

        for c in ("open", "high", "low"):
            out[c] = out[c].combine_first(out["close"])

        out["volume"] = out["volume"].fillna(0)

        out = out.dropna(subset=["close"]).copy()

        if out.empty:
            return out

        out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
        return out

    except Exception:
        logger.exception("[MTF HISTORY BOOTSTRAP] normalize summary df failed")
        return pd.DataFrame()


def attach_date_time_columns(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = ensure_df(df)
    if out.empty:
        return out

    try:
        out = normalize_higher_tf_datetime(out, interval=int(interval))

        if "source" not in out.columns:
            out["source"] = f"mtf_history_bootstrap_{int(interval)}min"

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] attach date/time failed interval=%s", interval, exc_info=True)

    return out


def latest_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = normalize_summary_df(df)
    if out.empty:
        return out

    try:
        interval = (
            int(out["interval"].dropna().iloc[0])
            if "interval" in out.columns and out["interval"].notna().any()
            else 1
        )

        out = drop_future_datetime_rows(out, interval=interval, label="latest_per_symbol")
        if out.empty:
            return out

        out = (
            out.sort_values(["symbol", "datetime"], kind="stable")
            .groupby("symbol", group_keys=False)
            .tail(1)
            .reset_index(drop=True)
        )

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] latest per symbol failed", exc_info=True)

    return out


__all__ = [
    "ensure_df",
    "coalesce_numeric",
    "normalize_symbol_value",
    "normalize_summary_df",
    "attach_date_time_columns",
    "latest_per_symbol",
]