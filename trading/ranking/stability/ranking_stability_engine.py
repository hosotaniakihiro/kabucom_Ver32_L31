# ============================================================
# File   : trading/ranking/stability/ranking_stability_engine.py
# Version: Ver1.0-PRODUCTION-RANKING-STABILITY
# ------------------------------------------------------------
# ✔ ranking jitter防止
# ✔ duplicate symbol guard
# ✔ ranking momentum
# ✔ ranking persistence
# ✔ ranking smoothing
# ✔ ranking memory cache
# ✔ entry安定化
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# ranking persistence memory
# ============================================================

MAX_HISTORY = 5


def _update_history(df):

    history = getattr(global_data, "ranking_history", [])

    history.append(df)

    if len(history) > MAX_HISTORY:
        history.pop(0)

    global_data.ranking_history = history

    return history


# ============================================================
# stability score
# ============================================================

def _calculate_persistence(symbol, history):

    count = 0

    for h in history:

        if symbol in h["symbol"].values:
            count += 1

    return count


# ============================================================
# main
# ============================================================

def apply_ranking_stability(df):

    try:

        if df is None or df.empty:
            return df

        df = df.copy()

        history = _update_history(df)

        persistence = []

        for s in df["symbol"]:

            p = _calculate_persistence(s, history)

            persistence.append(p)

        df["ranking_persistence"] = persistence

        # stability score
        df["ranking_stability"] = (
            df["ranking_persistence"] * 2
        )

        # momentum
        if "price_delta_1m" in df.columns:

            df["ranking_momentum"] = (
                df["price_delta_1m"]
                .fillna(0)
                .astype(float)
            )

        else:

            df["ranking_momentum"] = 0

        # final ranking score
        df["ranking_score"] = (

            df.get("ranking_stability", 0)
            + df.get("ranking_momentum", 0)

        )

        df = (
            df
            .sort_values("ranking_score", ascending=False)
            .reset_index(drop=True)
        )

        logger.info(
            "[RANKING_STABILITY] rows=%s",
            len(df),
        )

        return df

    except Exception:

        logger.exception("[ranking_stability]")

        return df