# ============================================================
# File   : ats/ats_ranking/builder.py
# Version: Ver1.0-ATS-RANKING-BUILDER
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from .constants import (
    TOP_N_INFLOW,
    TOP_N_GAINERS,
    TOP_N_LOSERS,
    TOP_N_TURNOVER,
    TOP_N_VOLUME_SPIKE,
    TOP_N_MARKET_GAINERS,
    TOP_N_MARKET_LOSERS,
    TOP_N_MARKET_VOLUME,
    CROSS_BASE_N,
    CROSS_OUT_N,
)
from .db_loader import _get_base_source_df
from .normalizer import _safe_symbol, _safe_numeric_series, _normalize_market_type
from .filters import _apply_hard_liquidity_filter
from .scoring import apply_scores

logger = logging.getLogger(__name__)


def _prepare_base_df() -> pd.DataFrame | None:
    df = _get_base_source_df()
    if df is None or df.empty:
        return None

    try:
        x = _safe_symbol(df.copy())
        if x.empty:
            return None

        if "current_price" not in x.columns and "price" in x.columns:
            x["current_price"] = x["price"]
        if "price" not in x.columns and "current_price" in x.columns:
            x["price"] = x["current_price"]

        if "trading_volume" not in x.columns and "volume" in x.columns:
            x["trading_volume"] = x["volume"]
        if "volume" not in x.columns and "trading_volume" in x.columns:
            x["volume"] = x["trading_volume"]

        if "volume_speed" not in x.columns and "volume_spike" in x.columns:
            x["volume_speed"] = x["volume_spike"]
        if "volume_spike" not in x.columns and "volume_speed" in x.columns:
            x["volume_spike"] = x["volume_speed"]

        if "market_type" not in x.columns and "market" in x.columns:
            x["market_type"] = x["market"]
        if "market" not in x.columns and "market_type" in x.columns:
            x["market"] = x["market_type"]

        if "turnover" not in x.columns and "trading_value" in x.columns:
            x["turnover"] = x["trading_value"]
        if "trading_value" not in x.columns and "turnover" in x.columns:
            x["trading_value"] = x["turnover"]

        if "rank_position" not in x.columns and "rank" in x.columns:
            x["rank_position"] = x["rank"]

        for c in [
            "rank_position",
            "trading_volume",
            "volume_speed",
            "rank_strength",
            "rank_persistence",
            "rank_delta",
            "price_delta_1m",
            "volume_delta_1m",
            "current_price",
            "turnover",
        ]:
            if c not in x.columns:
                x[c] = 0
            default_val = np.nan if c == "rank_position" else 0
            x[c] = _safe_numeric_series(x[c], default=default_val)

        if "rank_type" not in x.columns:
            x["rank_type"] = ""
        x["rank_type"] = x["rank_type"].astype(str).str.strip()

        if "market_type" not in x.columns:
            x["market_type"] = ""
        x["market_type"] = x["market_type"].map(_normalize_market_type)

        if "source" not in x.columns:
            x["source"] = ""

        if "snapshot_time" not in x.columns:
            x["snapshot_time"] = pd.NaT
        x["snapshot_time"] = pd.to_datetime(x["snapshot_time"], errors="coerce")

        logger.info(
            "[ATS RANKING] base profile rows=%d cols=%s rank_type_nonempty=%d market_nonempty=%d "
            "price_nonnull=%d volume_nonnull=%d vspd_nonnull=%d price>0=%d vol>=100=%d vspd>0=%d",
            len(x),
            list(x.columns),
            int((x["rank_type"].astype(str).str.strip() != "").sum()),
            int((x["market_type"].astype(str).str.strip() != "").sum()),
            int(x["current_price"].notna().sum()),
            int(x["trading_volume"].notna().sum()),
            int(x["volume_speed"].notna().sum()),
            int((x["current_price"] > 0).sum()),
            int((x["trading_volume"] >= 100).sum()),
            int((x["volume_speed"] > 0).sum()),
        )

        try:
            tmp = x.copy()
            tmp["__dt__"] = pd.to_datetime(tmp["snapshot_time"], errors="coerce")
            tmp = tmp.sort_values("__dt__", ascending=False, kind="mergesort")
            tmp = tmp.drop_duplicates(subset=["symbol", "rank_type"], keep="first")
            x = tmp.drop(columns=["__dt__"], errors="ignore")
        except Exception:
            logger.exception("prepare base df dedup symbol/rank_type latest failed")

        x = _apply_hard_liquidity_filter(x)
        if x is None or x.empty:
            logger.warning("[ATS RANKING] hard liquidity filter removed all")
            return None

        x = apply_scores(x)
        return x

    except Exception:
        logger.exception("prepare base df failed")
        return None


def build_ranking_candidates(max_symbols: int = 120) -> list[str]:
    try:
        from .selectors import (
            select_capital_inflow_symbols,
            select_top_gainers,
            select_top_losers,
            select_turnover_leaders,
            select_volume_spike_symbols,
            select_market_gainers,
            select_market_losers,
            select_market_volume_spike,
            _merge_keep_order,
        )
        from .cross_selectors import (
            select_turnover_leaders_within_gainers,
            select_gainers_within_turnover,
            select_losers_within_turnover,
        )

        inflow = select_capital_inflow_symbols(TOP_N_INFLOW)
        gainers = select_top_gainers(TOP_N_GAINERS)
        losers = select_top_losers(TOP_N_LOSERS)
        turnover = select_turnover_leaders(TOP_N_TURNOVER)
        volume_spike = select_volume_spike_symbols(TOP_N_VOLUME_SPIKE)

        turnover_within_gainers = select_turnover_leaders_within_gainers(CROSS_BASE_N, CROSS_OUT_N)
        gainers_within_turnover = select_gainers_within_turnover(CROSS_BASE_N, CROSS_OUT_N)
        losers_within_turnover = select_losers_within_turnover(CROSS_BASE_N, CROSS_OUT_N)

        prime_gainers = select_market_gainers("プライム", TOP_N_MARKET_GAINERS)
        prime_losers = select_market_losers("プライム", TOP_N_MARKET_LOSERS)
        prime_volume = select_market_volume_spike("プライム", TOP_N_MARKET_VOLUME)

        standard_gainers = select_market_gainers("スタンダード", TOP_N_MARKET_GAINERS)
        standard_losers = select_market_losers("スタンダード", TOP_N_MARKET_LOSERS)
        standard_volume = select_market_volume_spike("スタンダード", TOP_N_MARKET_VOLUME)

        growth_gainers = select_market_gainers("グロース", TOP_N_MARKET_GAINERS)
        growth_losers = select_market_losers("グロース", TOP_N_MARKET_LOSERS)
        growth_volume = select_market_volume_spike("グロース", TOP_N_MARKET_VOLUME)

        merged = _merge_keep_order(
            inflow,
            turnover_within_gainers,
            gainers_within_turnover,
            losers_within_turnover,
            gainers,
            losers,
            turnover,
            volume_spike,
            prime_gainers,
            prime_losers,
            prime_volume,
            standard_gainers,
            standard_losers,
            standard_volume,
            growth_gainers,
            growth_losers,
            growth_volume,
            max_symbols=max_symbols,
        )

        logger.info(
            "[ATS RANKING] inflow=%d gainers=%d losers=%d turnover=%d volume=%d "
            "cross(twg=%d gwt=%d lwt=%d) "
            "prime(g=%d l=%d v=%d) standard(g=%d l=%d v=%d) growth(g=%d l=%d v=%d) merged=%d",
            len(inflow),
            len(gainers),
            len(losers),
            len(turnover),
            len(volume_spike),
            len(turnover_within_gainers),
            len(gainers_within_turnover),
            len(losers_within_turnover),
            len(prime_gainers),
            len(prime_losers),
            len(prime_volume),
            len(standard_gainers),
            len(standard_losers),
            len(standard_volume),
            len(growth_gainers),
            len(growth_losers),
            len(growth_volume),
            len(merged),
        )

        return merged[:max_symbols]

    except Exception:
        logger.exception("build_ranking_candidates failed")
        return []