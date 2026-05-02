# ============================================================
# File   : trading/summary/filters/trade_universe_filter.py
# Version: PRODUCTION-STABLE-REV1.0-TRADE-UNIVERSE-GUARD
# ------------------------------------------------------------
# 【概要】
#   サマリー集計・スコア計算・TOP10表示・AI候補作成の前に、
#   売買対象外銘柄をDataFrameから除外する共通フィルタ。
#
# 【除外条件】
#   - BUY系:
#       slope <= 0.03
#       close <= 200
#
#   - 共通:
#       close <= 200
#
# 【重要】
#   200円以下は対象外:
#       close > 200 のみ通過
#
#   slope 0.03以下は対象外:
#       slope > 0.03 のみ通過
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


DEFAULT_MIN_BUY_SLOPE = 0.03
DEFAULT_MIN_PRICE = 200.0


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def get_min_buy_slope() -> float:
    """
    BUY対象の最低slope。

    slope <= 0.03 を除外するため、
    判定では slope > min_buy_slope を使う。
    """
    return _env_float("TRADE_UNIVERSE_MIN_BUY_SLOPE", DEFAULT_MIN_BUY_SLOPE)


def get_min_price() -> float:
    """
    最低株価。

    close <= 200 を除外するため、
    判定では close > min_price を使う。
    """
    return _env_float("TRADE_UNIVERSE_MIN_PRICE", DEFAULT_MIN_PRICE)


def _select_price_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    for c in (
        "close",
        "close_price",
        "price",
        "current_price",
        "last_price",
    ):
        if c in df.columns:
            return c

    return None


def _select_slope_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    for c in (
        "slope",
        "slope_atr_scaled",
        "score_slope",
        "disp_slope",
    ):
        if c in df.columns:
            return c

    return None


def apply_min_price_filter(
    df: pd.DataFrame,
    *,
    min_price: Optional[float] = None,
    context: str = "",
    log: Optional[logging.Logger] = None,
) -> pd.DataFrame:
    """
    close <= 200 を除外する共通フィルタ。

    集計対象から完全に外したい場合は、
    サマリー集計直後・スコア計算前にこれを使う。
    """
    lg = log or logger

    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    price_col = _select_price_col(out)
    min_price_v = float(min_price) if min_price is not None else get_min_price()

    before = len(out)

    if price_col is None:
        lg.warning(
            "[TRADE UNIVERSE FILTER] price column missing context=%s rows=%s cols=%s",
            context,
            before,
            list(out.columns),
        )
        return out

    out[price_col] = pd.to_numeric(out[price_col], errors="coerce").fillna(0.0)

    # 200円以下を除外。200円ちょうども除外。
    out = out[out[price_col] > min_price_v].copy()

    after = len(out)

    lg.info(
        "[TRADE UNIVERSE FILTER] min_price context=%s price_col=%s condition='%s > %.1f' "
        "before=%s after=%s skipped=%s",
        context,
        price_col,
        price_col,
        min_price_v,
        before,
        after,
        before - after,
    )

    return out


def apply_buy_slope_filter(
    df: pd.DataFrame,
    *,
    min_buy_slope: Optional[float] = None,
    context: str = "",
    log: Optional[logging.Logger] = None,
) -> pd.DataFrame:
    """
    BUY対象から slope <= 0.03 を除外する。

    注意:
      このフィルタは BUY候補向け。
      SELL集計まで同時に消したい場合は使う場所に注意。
    """
    lg = log or logger

    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    slope_col = _select_slope_col(out)
    min_slope_v = (
        float(min_buy_slope)
        if min_buy_slope is not None
        else get_min_buy_slope()
    )

    before = len(out)

    if slope_col is None:
        lg.warning(
            "[TRADE UNIVERSE FILTER] slope column missing context=%s rows=%s cols=%s",
            context,
            before,
            list(out.columns),
        )
        return out

    out[slope_col] = pd.to_numeric(out[slope_col], errors="coerce").fillna(0.0)

    # slope 0.03以下を除外。0.03ちょうども除外。
    out = out[out[slope_col] > min_slope_v].copy()

    after = len(out)

    lg.info(
        "[TRADE UNIVERSE FILTER] buy_slope context=%s slope_col=%s condition='%s > %.4f' "
        "before=%s after=%s skipped=%s",
        context,
        slope_col,
        slope_col,
        min_slope_v,
        before,
        after,
        before - after,
    )

    return out


def apply_buy_trade_universe_filter(
    df: pd.DataFrame,
    *,
    min_price: Optional[float] = None,
    min_buy_slope: Optional[float] = None,
    context: str = "",
    log: Optional[logging.Logger] = None,
) -> pd.DataFrame:
    """
    BUY候補・BUY TOP10・AI BUY候補向けの完全フィルタ。

    条件:
      close > 200
      slope > 0.03
    """
    lg = log or logger

    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    before = len(out)

    out = apply_min_price_filter(
        out,
        min_price=min_price,
        context=f"{context}:min_price",
        log=lg,
    )

    out = apply_buy_slope_filter(
        out,
        min_buy_slope=min_buy_slope,
        context=f"{context}:buy_slope",
        log=lg,
    )

    after = len(out)

    lg.info(
        "[TRADE UNIVERSE FILTER] buy_universe context=%s before=%s after=%s skipped=%s",
        context,
        before,
        after,
        before - after,
    )

    return out


def apply_common_trade_universe_filter(
    df: pd.DataFrame,
    *,
    min_price: Optional[float] = None,
    context: str = "",
    log: Optional[logging.Logger] = None,
) -> pd.DataFrame:
    """
    BUY/SELL共通の最低条件。

    現時点では close > 200 のみ。
    サマリー全体から200円以下を完全に外したい場合はこれを使う。
    """
    return apply_min_price_filter(
        df,
        min_price=min_price,
        context=context,
        log=log,
    )


__all__ = [
    "DEFAULT_MIN_BUY_SLOPE",
    "DEFAULT_MIN_PRICE",
    "get_min_buy_slope",
    "get_min_price",
    "apply_min_price_filter",
    "apply_buy_slope_filter",
    "apply_buy_trade_universe_filter",
    "apply_common_trade_universe_filter",
]