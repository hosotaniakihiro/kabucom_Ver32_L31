# ============================================================
# File   : trading/entry/summary_ai/pre_candidate_filters.py
# Version: Ver1.0-PRODUCTION-SUMMARY-AI-PRE-CANDIDATE-FILTERS
# ------------------------------------------------------------
# Purpose:
#   SUMMARY AI の TOP候補抽出前フィルター。
#
# Why:
#   trading/handlers/entry_filters.py の slope filter は発注直前ガード。
#   それだけだと、slope=0付近の銘柄が
#   TOP候補 / AI候補 / pending には一度入ってしまう。
#
# Policy:
#   ✔ TOP候補抽出前に slope が弱い銘柄を除外
#   ✔ BUY候補は slope >= ENTRY_MIN_BUY_SLOPE
#   ✔ SELL候補は slope <= ENTRY_MIN_SELL_SLOPE
#   ✔ slope_atr_scaled / slope / score_slope を優先順で見る
#   ✔ ENTRY_BYPASS_SLOPE_FILTER=1 で一時無効化可能
#   ✔ 既存DF列を壊さない
#   ✔ ログで before / after / skipped を確認可能
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# ENV helpers
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)

        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y", "on", "ok"):
            return True
        if s in ("0", "false", "no", "n", "off", "ng", ""):
            return False

        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return float(default)


def _safe_source(source: Any, default: str = "SUMMARY") -> str:
    try:
        s = str(source or default).strip().upper()
        return s if s else default
    except Exception:
        return default


# ============================================================
# Policy
# ============================================================

def allow_slope_filter_bypass() -> bool:
    return _env_bool("ENTRY_BYPASS_SLOPE_FILTER", False)


def min_buy_slope() -> float:
    return _env_float("ENTRY_MIN_BUY_SLOPE", 0.02)


def min_sell_slope() -> float:
    return _env_float("ENTRY_MIN_SELL_SLOPE", -0.02)


# ============================================================
# Slope resolver
# ============================================================

def resolve_slope_series(df: pd.DataFrame) -> pd.Series:
    """
    DFからENTRY判定用slopeを作る。

    優先:
      1. slope_atr_scaled
      2. slope
      3. score_slope

    注意:
      pandasでは 0.0 を欠損扱いしないように combine_first を使う。
    """

    if df is None or df.empty:
        return pd.Series(dtype="float64")

    result = pd.Series([pd.NA] * len(df), index=df.index, dtype="Float64")

    for col in ("slope_atr_scaled", "slope", "score_slope"):
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            result = result.combine_first(s)

    return pd.to_numeric(result, errors="coerce").fillna(0.0)


def resolve_side_series(df: pd.DataFrame, default_side: str = "BUY") -> pd.Series:
    """
    side列があれば使い、なければBUY扱い。
    SUMMARY AIの通常候補はBUY中心のため。
    """

    if df is None or df.empty:
        return pd.Series(dtype="object")

    for col in ("side", "entry_decision", "decision"):
        if col in df.columns:
            return df[col].fillna(default_side).astype(str).str.upper()

    return pd.Series([default_side] * len(df), index=df.index, dtype="object")


# ============================================================
# Public API
# ============================================================

def filter_summary_ai_candidates_before_top(
    df: pd.DataFrame,
    *,
    source: str = "SUMMARY",
    interval: int | str = 1,
    default_side: str = "BUY",
) -> pd.DataFrame:
    """
    TOP候補抽出前に slope が弱い銘柄を除外する。

    BUY:
      slope >= ENTRY_MIN_BUY_SLOPE

    SELL:
      slope <= ENTRY_MIN_SELL_SLOPE

    戻り値:
      フィルター済みDF
    """

    source_s = _safe_source(source)

    if df is None:
        logger.info(
            "[SUMMARY AI PRE FILTER] skip none df source=%s interval=%s",
            source_s,
            interval,
        )
        return pd.DataFrame()

    if df.empty:
        logger.info(
            "[SUMMARY AI PRE FILTER] skip empty df source=%s interval=%s",
            source_s,
            interval,
        )
        return df

    if allow_slope_filter_bypass():
        logger.warning(
            "[SUMMARY AI PRE FILTER] slope filter bypass source=%s interval=%s rows=%s",
            source_s,
            interval,
            len(df),
        )
        return df

    work = df.copy()

    slope = resolve_slope_series(work)
    side = resolve_side_series(work, default_side=default_side)

    buy_min = min_buy_slope()
    sell_max = min_sell_slope()

    buy_mask = (side == "BUY") & (slope >= buy_min)
    sell_mask = (side == "SELL") & (slope <= sell_max)

    # side不明はBUY扱いで判定
    unknown_mask = (~side.isin(["BUY", "SELL"])) & (slope >= buy_min)

    keep_mask = buy_mask | sell_mask | unknown_mask

    before = len(work)
    after = int(keep_mask.sum())
    skipped = before - after

    if skipped > 0:
        try:
            sample_cols = [
                c for c in (
                    "symbol",
                    "symbolname",
                    "name",
                    "close",
                    "close_price",
                    "score_buy",
                    "score",
                    "slope_atr_scaled",
                    "slope",
                    "score_slope",
                )
                if c in work.columns
            ]
            skipped_df = work.loc[~keep_mask, sample_cols].head(10)
            logger.info(
                "[SUMMARY AI PRE FILTER] slope skipped sample source=%s interval=%s skipped=%s sample=%s",
                source_s,
                interval,
                skipped,
                skipped_df.to_dict("records"),
            )
        except Exception:
            logger.debug("[SUMMARY AI PRE FILTER] skipped sample build failed", exc_info=True)

    logger.info(
        "[SUMMARY AI PRE FILTER] slope done source=%s interval=%s before=%s after=%s skipped=%s "
        "buy_min=%s sell_max=%s",
        source_s,
        interval,
        before,
        after,
        skipped,
        buy_min,
        sell_max,
    )

    return work.loc[keep_mask].copy().reset_index(drop=True)


__all__ = [
    "filter_summary_ai_candidates_before_top",
    "resolve_slope_series",
    "resolve_side_series",
    "allow_slope_filter_bypass",
    "min_buy_slope",
    "min_sell_slope",
]