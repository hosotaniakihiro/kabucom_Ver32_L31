# ============================================================
# File   : trading/summary/indicators/atr_slope_safe.py
# Version: PRODUCTION-STABLE-REV1.0
# Purpose:
#   PUSH/Yahoo由来OHLC用の ATR / slope / slope_atr_scaled を安全に計算する
#
# Important:
#   - ranking_summary には本物ATRとして使わない
#   - OHLCが存在する stock_summary 用
#   - ATR=0 による slope_atr_scaled 暴走を防ぐ
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _ensure_col(df: pd.DataFrame, col: str, default=np.nan) -> None:
    if col not in df.columns:
        df[col] = default


def add_atr_and_slope_safe(
    df: pd.DataFrame,
    *,
    atr_period: int = 14,
    slope_period: int = 5,
    price_col: str = "close",
    high_col: str = "high",
    low_col: str = "low",
    group_col: str = "symbol",
    datetime_col: str = "datetime",
    overwrite: bool = True,
) -> pd.DataFrame:
    """
    ATR / slope / slope_atr_scaled を安全に追加する。

    Parameters
    ----------
    df:
        OHLCを持つDataFrame。
    atr_period:
        ATR期間。通常14。
    slope_period:
        slope計算に使う本数。通常5。
    price_col:
        終値列。
    high_col:
        高値列。
    low_col:
        安値列。
    group_col:
        銘柄列。
    datetime_col:
        時刻列。
    overwrite:
        Trueなら既存 atr/slope/slope_atr_scaled を再計算して上書き。

    Returns
    -------
    pd.DataFrame
    """

    if df is None or df.empty:
        return df

    out = df.copy()

    _ensure_col(out, group_col, "UNKNOWN")
    _ensure_col(out, price_col, np.nan)
    _ensure_col(out, high_col, np.nan)
    _ensure_col(out, low_col, np.nan)

    if datetime_col in out.columns:
        out[datetime_col] = pd.to_datetime(out[datetime_col], errors="coerce")
        out = out.sort_values([group_col, datetime_col])

    out[group_col] = out[group_col].astype(str)

    out[price_col] = _to_numeric(out[price_col])
    out[high_col] = _to_numeric(out[high_col])
    out[low_col] = _to_numeric(out[low_col])

    # open_price/high_price/low_price/close_price しかない場合の補完
    alias_map = {
        price_col: ["close_price", "current_price", "price"],
        high_col: ["high_price"],
        low_col: ["low_price"],
    }

    for target_col, aliases in alias_map.items():
        if out[target_col].isna().all():
            for alias in aliases:
                if alias in out.columns:
                    out[target_col] = _to_numeric(out[alias])
                    break

    if not overwrite:
        required = {"atr", "slope", "slope_atr_scaled"}
        if required.issubset(out.columns):
            return out

    prev_close_col = "prev_close"
    tr_col = "tr"

    out[prev_close_col] = out.groupby(group_col)[price_col].shift(1)

    tr1 = out[high_col] - out[low_col]
    tr2 = (out[high_col] - out[prev_close_col]).abs()
    tr3 = (out[low_col] - out[prev_close_col]).abs()

    out[tr_col] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # 初回バーで prev_close がない場合は high-low を使う
    out[tr_col] = out[tr_col].fillna(tr1)

    # Wilder ATRに近い ewm
    out["atr"] = (
        out.groupby(group_col)[tr_col]
        .transform(
            lambda s: s.ewm(
                alpha=1.0 / float(atr_period),
                adjust=False,
                min_periods=3,
            ).mean()
        )
    )

    out["slope"] = (
        out.groupby(group_col)[price_col]
        .transform(lambda s: (s - s.shift(slope_period)) / float(slope_period))
    )

    safe_atr = out["atr"].replace(0, np.nan)
    out["slope_atr_scaled"] = out["slope"] / safe_atr

    out["atr"] = out["atr"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out["slope"] = out["slope"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out["slope_atr_scaled"] = (
        out["slope_atr_scaled"]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
    )

    # 互換用
    out["atr_1m"] = out.get("atr_1m", out["atr"])

    logger.debug(
        "[ATR SLOPE SAFE] rows=%s symbols=%s atr_nonzero=%s slope_nonzero=%s scaled_nonzero=%s",
        len(out),
        out[group_col].nunique() if group_col in out.columns else 0,
        int((out["atr"] != 0).sum()),
        int((out["slope"] != 0).sum()),
        int((out["slope_atr_scaled"] != 0).sum()),
    )

    return out


__all__ = [
    "add_atr_and_slope_safe",
]