# ============================================================
# File   : trading/ranking/freshness_filter.py
# ------------------------------------------------------------
# ✔ PUSH freshness 判定
# ✔ ranking前フィルタ専用
# ✔ 実運用安定版
# ============================================================

from __future__ import annotations

import pandas as pd
import logging

logger = logging.getLogger(__name__)


def filter_fresh_push_symbols(
    df_1m: pd.DataFrame,
    freshness_minutes: int = 2,
) -> pd.DataFrame:

    if df_1m is None or df_1m.empty:
        return df_1m

    now = pd.Timestamp.now().floor("min")
    threshold = now - pd.Timedelta(minutes=freshness_minutes)

    if "source" not in df_1m.columns:
        logger.warning("[FRESH] source column missing")
        return pd.DataFrame()

    df_push = df_1m[df_1m["source"] == "push"]

    if df_push.empty:
        return pd.DataFrame()

    df_last = (
        df_push
        .sort_values("datetime")
        .groupby("symbol")
        .tail(1)
    )

    df_fresh = df_last[df_last["datetime"] >= threshold]

    fresh_symbols = df_fresh["symbol"].unique()

    filtered = df_1m[df_1m["symbol"].isin(fresh_symbols)].copy()

    logger.info(
        "[FRESH] symbols before=%d after=%d",
        df_1m["symbol"].nunique(),
        filtered["symbol"].nunique(),
    )

    return filtered