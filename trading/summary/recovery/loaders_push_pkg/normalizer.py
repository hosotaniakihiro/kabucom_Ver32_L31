# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/normalizer.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-NORMALIZER
# ------------------------------------------------------------
# 【概要】
#   PUSH dataframe 正規化
#
# 【主な機能】
#   ✔ symbol 正規化
#   ✔ tick_time 列名差異吸収
#   ✔ price 列名差異吸収
#   ✔ cum_volume 列名差異吸収
#   ✔ symbolname 列名差異吸収
#   ✔ duplicate column guard
#   ✔ tick_time tz-naive 化
#   ✔ price <= 0 除外
#
# 【重要】
#   - DB上の列名が datetime / time / CurrentPriceTime 等でも tick_time に統一
#   - UTC変換はしない
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

from trading.summary.recovery.helpers import (
    ensure_dataframe,
    normalize_symbol,
    safe_get_series,
)

from .timezone import to_tz_naive_datetime_series

logger = logging.getLogger(__name__)


def normalize_symbols(values: Optional[Iterable]) -> list[str]:
    if values is None:
        return []

    out: list[str] = []
    seen: set[str] = set()

    for v in values:
        try:
            s = str(v).strip()
            if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
                continue
            s = s.replace(".0", "")
            s = s.replace("　", "").replace(" ", "")
            s = s.upper()
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        except Exception:
            continue

    return out


def drop_duplicate_columns(df: pd.DataFrame, *, label: str) -> pd.DataFrame:
    try:
        if df is None or getattr(df, "empty", True):
            return df

        if not hasattr(df, "columns"):
            return df

        dup_mask = df.columns.duplicated()
        if not dup_mask.any():
            return df

        dup_cols = df.columns[dup_mask].tolist()
        logger.warning(
            "[summary.recovery.loaders_push.normalizer] duplicate columns dropped label=%s dup_cols=%s",
            label,
            dup_cols,
        )
        return df.loc[:, ~df.columns.duplicated()].copy()

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_push.normalizer] drop_duplicate_columns failed label=%s",
            label,
        )
        return df


def apply_symbol_filter_df(df: pd.DataFrame, symbols: Optional[Iterable[str]]) -> pd.DataFrame:
    symbol_list = normalize_symbols(symbols)
    if not symbol_list:
        return df

    if df is None or df.empty or "symbol" not in df.columns:
        return df

    try:
        return df.loc[df["symbol"].astype(str).isin(symbol_list)].copy().reset_index(drop=True)
    except Exception:
        logger.exception("[summary.recovery.loaders_push.normalizer] dataframe symbol filter failed")
        return df


def normalize_push_df(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_dataframe(df)
    if out.empty:
        return out

    try:
        out = drop_duplicate_columns(out.copy(), label="normalize_push_df")

        rename_map = {
            # symbol
            "Symbol": "symbol",
            "symbol_code": "symbol",
            "code": "symbol",

            # time
            "CurrentPriceTime": "tick_time",
            "current_price_time": "tick_time",
            "current_time": "tick_time",
            "datetime": "tick_time",
            "time": "tick_time",
            "timestamp": "tick_time",
            "received_at": "tick_time",
            "inserted_at": "tick_time",
            "created_at": "tick_time",

            # price
            "CurrentPrice": "price",
            "current_price": "price",
            "last_price": "price",
            "LastPrice": "price",
            "Price": "price",

            # volume
            "TradingVolume": "cum_volume",
            "Volume": "cum_volume",
            "trading_volume": "cum_volume",

            # name
            "SymbolName": "symbolname",
            "name": "symbolname",
        }

        for src, dst in rename_map.items():
            if src in out.columns and dst not in out.columns:
                s = safe_get_series(out, src)
                if s is not None:
                    out[dst] = s

        out = normalize_symbol(out)

        if "tick_time" not in out.columns:
            logger.warning(
                "[summary.recovery.loaders_push.normalizer] push df has no tick_time-compatible column columns=%s",
                list(out.columns),
            )
            return pd.DataFrame()

        tick_s = safe_get_series(out, "tick_time")
        out["tick_time"] = to_tz_naive_datetime_series(
            tick_s,
            label="normalize_push_df.tick_time",
        )
        out = out.dropna(subset=["tick_time"]).copy()

        if "price" not in out.columns:
            out["price"] = pd.NA

        out["price"] = pd.to_numeric(safe_get_series(out, "price"), errors="coerce")
        out["price"] = out["price"].replace([float("inf"), float("-inf")], pd.NA)
        out["price"] = out["price"].mask(pd.to_numeric(out["price"], errors="coerce") <= 0)

        if "cum_volume" in out.columns:
            out["cum_volume"] = pd.to_numeric(
                safe_get_series(out, "cum_volume"),
                errors="coerce",
            )
        else:
            out["cum_volume"] = pd.NA

        if "symbolname" not in out.columns:
            out["symbolname"] = ""

        out = out.dropna(subset=["symbol", "price"]).copy()

        if out.empty:
            return out

        out["symbol"] = out["symbol"].astype(str).str.strip()
        out = out.loc[out["symbol"] != ""].copy()

        out = out.sort_values(
            ["symbol", "tick_time"],
            kind="stable",
        ).reset_index(drop=True)

        return out

    except Exception:
        logger.exception("[summary.recovery.loaders_push.normalizer] normalize_push_df failed")
        return pd.DataFrame()


# ------------------------------------------------------------
# Backward-compatible aliases
# ------------------------------------------------------------
_drop_duplicate_columns = drop_duplicate_columns
_apply_symbol_filter_df = apply_symbol_filter_df


__all__ = [
    "normalize_symbols",
    "drop_duplicate_columns",
    "apply_symbol_filter_df",
    "normalize_push_df",
    "_drop_duplicate_columns",
    "_apply_symbol_filter_df",
]