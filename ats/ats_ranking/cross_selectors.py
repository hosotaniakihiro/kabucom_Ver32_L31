# ============================================================
# File   : ats/ats_ranking/cross_selectors.py
# Version: Ver1.0-ATS-RANKING-CROSS-SELECTORS
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .builder import _prepare_base_df
from .normalizer import _safe_symbol, _safe_numeric_series, _unique_keep_order

logger = logging.getLogger(__name__)


def _build_ranktype_pivot(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        x = df.copy()
        x = _safe_symbol(x)
        if x.empty:
            return pd.DataFrame()

        if "rank_type" not in x.columns:
            x["rank_type"] = ""
        if "rank_position" not in x.columns:
            x["rank_position"] = float("nan")
        if "snapshot_time" not in x.columns:
            x["snapshot_time"] = pd.NaT
        if "trading_volume" not in x.columns:
            x["trading_volume"] = 0
        if "price_delta_1m" not in x.columns:
            x["price_delta_1m"] = 0
        if "volume_speed" not in x.columns:
            x["volume_speed"] = 0
        if "market_type" not in x.columns:
            x["market_type"] = ""

        x["rank_type"] = x["rank_type"].astype(str).str.strip()
        x["rank_position"] = _safe_numeric_series(x["rank_position"], default=float("nan"))
        x["snapshot_time"] = pd.to_datetime(x["snapshot_time"], errors="coerce")
        x["trading_volume"] = _safe_numeric_series(x["trading_volume"], default=0)
        x["price_delta_1m"] = _safe_numeric_series(x["price_delta_1m"], default=0)
        x["volume_speed"] = _safe_numeric_series(x["volume_speed"], default=0)
        x["market_type"] = x["market_type"].astype(str).str.strip()

        latest_cols = ["symbol", "symbolname", "market_type", "trading_volume", "price_delta_1m", "volume_speed", "snapshot_time"]
        latest_existing = [c for c in latest_cols if c in x.columns]

        latest = (
            x.sort_values("snapshot_time", ascending=False, kind="mergesort")
             .drop_duplicates(subset=["symbol"], keep="first")[latest_existing]
             .copy()
        )

        pivot = (
            x.pivot_table(
                index="symbol",
                columns="rank_type",
                values="rank_position",
                aggfunc="min"
            )
            .reset_index()
        )

        pivot = pivot.merge(latest, on="symbol", how="left")

        rename_map = {
            "値上がり率": "rank_up",
            "値下がり率": "rank_down",
            "売買代金": "rank_turnover",
            "売買代金急増": "rank_turnover_surge",
            "売買高上位": "rank_volume",
            "売買高急増": "rank_volume_surge",
            "TICK回数": "rank_tick",
        }
        pivot = pivot.rename(columns=rename_map)

        return pivot

    except Exception:
        logger.exception("build ranktype pivot failed")
        return pd.DataFrame()


def select_turnover_leaders_within_gainers(base_n: int = 50, out_n: int = 20, market_type: str = "") -> list[str]:
    df = _prepare_base_df()
    p = _build_ranktype_pivot(df)
    if p.empty or "rank_up" not in p.columns:
        return []

    try:
        x = p.copy()

        if market_type and "market_type" in x.columns:
            x = x[x["market_type"].astype(str).str.strip() == str(market_type)].copy()

        x = x[x["rank_up"].notna()].copy()
        if x.empty:
            return []

        x["rank_up"] = _safe_numeric_series(x["rank_up"], default=float("nan"))
        x["trading_volume"] = _safe_numeric_series(x.get("trading_volume", pd.Series(index=x.index)), default=0)
        x["price_delta_1m"] = _safe_numeric_series(x.get("price_delta_1m", pd.Series(index=x.index)), default=0)

        x = x.sort_values(["rank_up", "price_delta_1m"], ascending=[True, False], kind="mergesort").head(base_n)

        if "rank_turnover" in x.columns:
            x["rank_turnover"] = _safe_numeric_series(x["rank_turnover"], default=float("nan"))
            x = x.sort_values(["rank_turnover", "trading_volume", "rank_up"], ascending=[True, False, True], kind="mergesort")
        else:
            x = x.sort_values(["trading_volume", "rank_up"], ascending=[False, True], kind="mergesort")

        return _unique_keep_order(x["symbol"].astype(str).tolist())[:out_n]

    except Exception:
        logger.exception("select_turnover_leaders_within_gainers failed")
        return []


def select_gainers_within_turnover(base_n: int = 50, out_n: int = 20, market_type: str = "") -> list[str]:
    df = _prepare_base_df()
    p = _build_ranktype_pivot(df)
    if p.empty or "rank_turnover" not in p.columns:
        return []

    try:
        x = p.copy()

        if market_type and "market_type" in x.columns:
            x = x[x["market_type"].astype(str).str.strip() == str(market_type)].copy()

        x = x[x["rank_turnover"].notna()].copy()
        if x.empty:
            return []

        x["rank_turnover"] = _safe_numeric_series(x["rank_turnover"], default=float("nan"))
        x["trading_volume"] = _safe_numeric_series(x.get("trading_volume", pd.Series(index=x.index)), default=0)
        x["price_delta_1m"] = _safe_numeric_series(x.get("price_delta_1m", pd.Series(index=x.index)), default=0)

        x = x.sort_values(["rank_turnover", "trading_volume"], ascending=[True, False], kind="mergesort").head(base_n)

        if "rank_up" in x.columns:
            x = x[x["rank_up"].notna()].copy()
            if x.empty:
                return []
            x["rank_up"] = _safe_numeric_series(x["rank_up"], default=float("nan"))
            x = x.sort_values(["rank_up", "rank_turnover", "price_delta_1m"], ascending=[True, True, False], kind="mergesort")
        else:
            x = x.sort_values(["price_delta_1m", "rank_turnover"], ascending=[False, True], kind="mergesort")

        return _unique_keep_order(x["symbol"].astype(str).tolist())[:out_n]

    except Exception:
        logger.exception("select_gainers_within_turnover failed")
        return []


def select_losers_within_turnover(base_n: int = 50, out_n: int = 20, market_type: str = "") -> list[str]:
    df = _prepare_base_df()
    p = _build_ranktype_pivot(df)
    if p.empty or "rank_turnover" not in p.columns:
        return []

    try:
        x = p.copy()

        if market_type and "market_type" in x.columns:
            x = x[x["market_type"].astype(str).str.strip() == str(market_type)].copy()

        x = x[x["rank_turnover"].notna()].copy()
        if x.empty:
            return []

        x["rank_turnover"] = _safe_numeric_series(x["rank_turnover"], default=float("nan"))
        x["trading_volume"] = _safe_numeric_series(x.get("trading_volume", pd.Series(index=x.index)), default=0)
        x["price_delta_1m"] = _safe_numeric_series(x.get("price_delta_1m", pd.Series(index=x.index)), default=0)

        x = x.sort_values(["rank_turnover", "trading_volume"], ascending=[True, False], kind="mergesort").head(base_n)

        if "rank_down" in x.columns:
            x = x[x["rank_down"].notna()].copy()
            if x.empty:
                return []
            x["rank_down"] = _safe_numeric_series(x["rank_down"], default=float("nan"))
            x = x.sort_values(["rank_down", "rank_turnover", "price_delta_1m"], ascending=[True, True, True], kind="mergesort")
        else:
            x = x.sort_values(["price_delta_1m", "rank_turnover"], ascending=[True, True], kind="mergesort")

        return _unique_keep_order(x["symbol"].astype(str).tolist())[:out_n]

    except Exception:
        logger.exception("select_losers_within_turnover failed")
        return []