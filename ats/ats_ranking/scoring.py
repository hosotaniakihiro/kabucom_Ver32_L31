# ============================================================
# File   : ats/ats_ranking/scoring.py
# Version: Ver1.0-ATS-RANKING-SCORING
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd


def apply_scores(x: pd.DataFrame) -> pd.DataFrame:
    if x is None or x.empty:
        return x

    rank_base = x["rank_position"].replace(0, np.nan)
    x["rank_inverse_score"] = (1.0 / rank_base).replace([np.inf, -np.inf], np.nan).fillna(0)

    rt = x["rank_type"].astype(str)

    x["gainer_score"] = np.where(rt.str.contains("値上がり", na=False), x["rank_inverse_score"], 0.0)
    x["loser_score"] = np.where(rt.str.contains("値下がり", na=False), x["rank_inverse_score"], 0.0)
    x["turnover_score"] = np.where(
        rt.str.contains("売買代金", na=False),
        x["rank_inverse_score"] + np.log1p(x["trading_volume"].clip(lower=0)),
        0.0,
    )
    x["volume_score"] = np.where(
        rt.str.contains("売買高", na=False) | rt.str.contains("TICK", na=False),
        x["rank_inverse_score"] + x["volume_speed"].clip(lower=0),
        0.0,
    )

    x["inflow_score"] = (
        x["turnover_score"].fillna(0)
        + x["volume_score"].fillna(0)
        + x["gainer_score"].fillna(0)
        + x["rank_strength"].fillna(0)
        + x["rank_persistence"].fillna(0)
        + x["rank_delta"].clip(lower=0).fillna(0)
        + x["price_delta_1m"].clip(lower=0).fillna(0)
        + x["volume_delta_1m"].clip(lower=0).fillna(0)
    )

    if "pct_change" not in x.columns:
        x["pct_change"] = 0.0
        x.loc[rt.str.contains("値上がり", na=False), "pct_change"] = x.loc[
            rt.str.contains("値上がり", na=False), "rank_inverse_score"
        ]
        x.loc[rt.str.contains("値下がり", na=False), "pct_change"] = -x.loc[
            rt.str.contains("値下がり", na=False), "rank_inverse_score"
        ]

    if "capital_score" not in x.columns:
        x["capital_score"] = x["pct_change"].fillna(0) * np.log1p(x["turnover"].clip(lower=0))

    if "volume_spike" not in x.columns:
        x["volume_spike"] = (
            x["volume_speed"].fillna(0)
            + x["volume_delta_1m"].clip(lower=0).fillna(0)
            + x["volume_score"].fillna(0)
        )

    if "total_score" not in x.columns:
        x["total_score"] = x["inflow_score"]

    return x