# ============================================================
# File   : trading/ranking/summary/bootstrap_resample.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-RESAMPLE
# ------------------------------------------------------------
# 【概要】
#   ranking summary 1min から 3min / 5min を作成
#
# 【重要方針】
#   3min / 5min もランキング由来では OHLC 同値
#   open = high = low = close = 区間最後の close
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from trading.ranking.summary.bootstrap_ohlcv import (
    normalize_datetime,
    normalize_symbol,
)

logger = logging.getLogger(__name__)


def _join_rank_types(s: pd.Series) -> str:
    vals: list[str] = []

    for v in s.astype(str).tolist():
        for part in str(v).split(","):
            part = part.strip()
            if part and part not in vals and part not in ("nan", "None", "<NA>"):
                vals.append(part)

    return ",".join(vals)


def resample_ranking_summary(base_1m: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if base_1m is None or base_1m.empty:
        return pd.DataFrame()

    if int(interval) == 1:
        out = base_1m.copy()
        out["interval"] = 1
        return out

    x = base_1m.copy()
    x = normalize_symbol(x)
    x = normalize_datetime(x)

    if x.empty:
        return pd.DataFrame()

    x["bucket"] = x["datetime"].dt.floor(f"{int(interval)}min")
    x = x.sort_values(["symbol", "bucket", "datetime"]).copy()

    grouped = x.groupby(["symbol", "bucket"], sort=False)

    agg_map = {
        "symbolname": ("symbolname", "last"),
        "close": ("close", "last"),
        "volume": ("volume", "max"),
        "trading_value": ("trading_value", "max"),
        "tick_count": ("tick_count", "max"),
        "turnover": ("turnover", "last"),
        "best_rank_position": ("best_rank_position", "min"),
        "last_rank_position": ("last_rank_position", "last"),
        "avg_rank_position": ("avg_rank_position", "mean"),
        "rank_count": ("rank_count", "sum"),
        "rank_types": ("rank_types", _join_rank_types),
    }

    usable_agg = {}
    for out_col, spec in agg_map.items():
        src_col = spec[0]
        if src_col in x.columns:
            usable_agg[out_col] = spec

    out = grouped.agg(**usable_agg).reset_index()
    out.rename(columns={"bucket": "datetime"}, inplace=True)

    if "close" not in out.columns:
        out["close"] = pd.Series(np.nan, index=out.index, dtype="float64")

    out["open"] = out["close"]
    out["high"] = out["close"]
    out["low"] = out["close"]

    out["interval"] = int(interval)
    out["source"] = "ranking_snapshot"
    out["updated_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info(
        "[RANKING SUMMARY BOOTSTRAP RESAMPLE] %smin built rows=%d symbols=%d dt_min=%s dt_max=%s",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["datetime"].min() if not out.empty else None,
        out["datetime"].max() if not out.empty else None,
    )

    return out


__all__ = [
    "resample_ranking_summary",
]