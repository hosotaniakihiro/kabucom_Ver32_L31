# ============================================================
# File   : ats/ats_ranking/selectors.py
# Version: Ver1.0-ATS-RANKING-SELECTORS
# ============================================================

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from .builder import _prepare_base_df
from .constants import (
    TOP_N_INFLOW,
    TOP_N_GAINERS,
    TOP_N_LOSERS,
    TOP_N_TURNOVER,
    TOP_N_VOLUME_SPIKE,
    TOP_N_MARKET_GAINERS,
    TOP_N_MARKET_LOSERS,
    TOP_N_MARKET_VOLUME,
)
from .normalizer import _safe_symbol, _safe_numeric_series, _unique_keep_order, _normalize_symbol

logger = logging.getLogger(__name__)


def _select_sorted_symbols(df, sort_col: str, ascending: bool, max_symbols: int) -> List[str]:
    if df is None or df.empty or sort_col not in df.columns:
        return []

    try:
        x = _safe_symbol(df.copy())
        x[sort_col] = _safe_numeric_series(x[sort_col], default=0)
        x = x.sort_values(sort_col, ascending=ascending, kind="mergesort")
        symbols = x["symbol"].astype(str).tolist()
        return _unique_keep_order([s for s in symbols if s])[:max_symbols]
    except Exception:
        logger.exception("sorted selection failed: %s", sort_col)
        return []


def _select_market_sorted_symbols(df, market_type: str, sort_col: str, ascending: bool, max_symbols: int) -> List[str]:
    if df is None or df.empty or "market_type" not in df.columns:
        return []

    try:
        x = df.copy()
        x["market_type"] = x["market_type"].astype(str).str.strip()
        x = x[x["market_type"] == str(market_type)].copy()
        if x.empty:
            return []

        return _select_sorted_symbols(x, sort_col=sort_col, ascending=ascending, max_symbols=max_symbols)
    except Exception:
        logger.exception("market sorted selection failed: %s / %s", market_type, sort_col)
        return []


def _select_by_rank_type(
    df,
    rank_type_keyword: str,
    max_symbols: int,
    market_type: str = "",
    extra_sort_cols: Optional[List[Tuple[str, bool]]] = None,
) -> List[str]:
    if df is None or df.empty or "rank_type" not in df.columns:
        return []

    try:
        x = df.copy()
        x["rank_type"] = x["rank_type"].astype(str).str.strip()

        if market_type:
            x["market_type"] = x["market_type"].astype(str).str.strip()
            x = x[x["market_type"] == market_type].copy()

        x = x[x["rank_type"].str.contains(rank_type_keyword, na=False)].copy()
        if x.empty:
            return []

        sort_cols = ["rank_position"]
        ascending = [True]

        if extra_sort_cols:
            for col, asc in extra_sort_cols:
                if col in x.columns:
                    sort_cols.append(col)
                    ascending.append(asc)

        for c in sort_cols:
            if c in x.columns:
                default_val = float("nan") if c == "rank_position" else 0
                x[c] = _safe_numeric_series(x[c], default=default_val)

        x = x.sort_values(sort_cols, ascending=ascending, kind="mergesort")
        symbols = x["symbol"].astype(str).tolist()
        return _unique_keep_order(symbols)[:max_symbols]

    except Exception:
        logger.exception("select by rank type failed: %s / %s", rank_type_keyword, market_type)
        return []


def _merge_keep_order(*groups: List[str], max_symbols: int) -> List[str]:
    merged: List[str] = []
    for g in groups:
        if g:
            merged.extend([_normalize_symbol(s) for s in g if _normalize_symbol(s)])
    return _unique_keep_order(merged)[:max_symbols]


def select_capital_inflow_symbols(max_symbols: int = TOP_N_INFLOW) -> List[str]:
    df = _prepare_base_df()
    if df is None:
        return []

    inflow_from_rank = _select_by_rank_type(
        df,
        rank_type_keyword="売買代金急増",
        max_symbols=max_symbols,
        extra_sort_cols=[("rank_strength", False), ("rank_persistence", False)],
    )
    if inflow_from_rank:
        return inflow_from_rank

    return _select_sorted_symbols(df=df, sort_col="total_score", ascending=False, max_symbols=max_symbols)


def select_top_gainers(max_symbols: int = TOP_N_GAINERS) -> List[str]:
    df = _prepare_base_df()
    if df is None:
        return []

    gainers = _select_by_rank_type(
        df,
        rank_type_keyword="値上がり",
        max_symbols=max_symbols,
        extra_sort_cols=[("price_delta_1m", False), ("rank_strength", False)],
    )
    if gainers:
        return gainers

    return _select_sorted_symbols(df=df, sort_col="pct_change", ascending=False, max_symbols=max_symbols)


def select_top_losers(max_symbols: int = TOP_N_LOSERS) -> List[str]:
    df = _prepare_base_df()
    if df is None:
        return []

    losers = _select_by_rank_type(
        df,
        rank_type_keyword="値下がり",
        max_symbols=max_symbols,
        extra_sort_cols=[("price_delta_1m", True), ("rank_strength", False)],
    )
    if losers:
        return losers

    return _select_sorted_symbols(df=df, sort_col="pct_change", ascending=True, max_symbols=max_symbols)


def select_turnover_leaders(max_symbols: int = TOP_N_TURNOVER) -> List[str]:
    df = _prepare_base_df()
    if df is None:
        return []

    leaders = _select_by_rank_type(
        df,
        rank_type_keyword="売買代金",
        max_symbols=max_symbols,
        extra_sort_cols=[("trading_volume", False), ("rank_strength", False)],
    )
    if leaders:
        return leaders

    return _select_sorted_symbols(df=df, sort_col="turnover", ascending=False, max_symbols=max_symbols)


def select_volume_spike_symbols(max_symbols: int = TOP_N_VOLUME_SPIKE) -> List[str]:
    df = _prepare_base_df()
    if df is None:
        return []

    volume_syms = _select_by_rank_type(
        df,
        rank_type_keyword="売買高急増|売買高上位|TICK",
        max_symbols=max_symbols,
        extra_sort_cols=[("volume_speed", False), ("volume_delta_1m", False)],
    )
    if volume_syms:
        return volume_syms

    return _select_sorted_symbols(df=df, sort_col="volume_spike", ascending=False, max_symbols=max_symbols)


def select_market_gainers(market_type: str, max_symbols: int = TOP_N_MARKET_GAINERS) -> List[str]:
    df = _prepare_base_df()
    if df is None:
        return []

    gainers = _select_by_rank_type(
        df,
        rank_type_keyword="値上がり",
        market_type=market_type,
        max_symbols=max_symbols,
        extra_sort_cols=[("price_delta_1m", False), ("rank_strength", False)],
    )
    if gainers:
        return gainers

    return _select_market_sorted_symbols(df=df, market_type=market_type, sort_col="pct_change", ascending=False, max_symbols=max_symbols)


def select_market_losers(market_type: str, max_symbols: int = TOP_N_MARKET_LOSERS) -> List[str]:
    df = _prepare_base_df()
    if df is None:
        return []

    losers = _select_by_rank_type(
        df,
        rank_type_keyword="値下がり",
        market_type=market_type,
        max_symbols=max_symbols,
        extra_sort_cols=[("price_delta_1m", True), ("rank_strength", False)],
    )
    if losers:
        return losers

    return _select_market_sorted_symbols(df=df, market_type=market_type, sort_col="pct_change", ascending=True, max_symbols=max_symbols)


def select_market_volume_spike(market_type: str, max_symbols: int = TOP_N_MARKET_VOLUME) -> List[str]:
    df = _prepare_base_df()
    if df is None:
        return []

    vol = _select_by_rank_type(
        df,
        rank_type_keyword="売買高急増|売買高上位|TICK",
        market_type=market_type,
        max_symbols=max_symbols,
        extra_sort_cols=[("volume_speed", False), ("volume_delta_1m", False)],
    )
    if vol:
        return vol

    return _select_market_sorted_symbols(df=df, market_type=market_type, sort_col="volume_spike", ascending=False, max_symbols=max_symbols)