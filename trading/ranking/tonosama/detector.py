# ============================================================
# File   : trading/ranking/tonosama/detector.py
# Purpose: ランキング主導 殿様イナゴ判定
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .config import RankingTonosamaConfig, DEFAULT_RANKING_TONOSAMA_CONFIG

logger = logging.getLogger(__name__)


def detect_ranking_tonosama(
    df: pd.DataFrame,
    config: RankingTonosamaConfig = DEFAULT_RANKING_TONOSAMA_CONFIG,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    defaults = {
        "price_change_1m_pct": 0.0,
        "price_change_3m_pct": 0.0,
        "price_change_5m_pct": 0.0,
        "volume_delta_1m": 0.0,
        "volume_spike_ratio": 0.0,
        "_rank": 9999,
        "rank_up_speed": 0,
        "first_appearance": False,
        "ranking_category_count": 1,
        "from_recent_high_5m_pct": 0.0,
        "ranking_tonosama_raw_score": 0.0,
    }

    for col, val in defaults.items():
        if col not in out.columns:
            out[col] = val

    price_ok = (
        (out["price_change_1m_pct"] >= config.min_price_change_1m_pct) |
        (out["price_change_3m_pct"] >= config.min_price_change_3m_pct)
    )

    price_not_too_late = out["price_change_5m_pct"].fillna(0.0) <= config.max_price_change_5m_pct

    volume_ok = (
        (out["volume_delta_1m"] >= config.min_volume_delta_1m) |
        (out["volume_spike_ratio"] >= config.min_volume_spike_ratio)
    )

    rank_ok = (
        (out["_rank"] <= config.max_rank) |
        (out["rank_up_speed"] >= config.rank_up_threshold) |
        (out["first_appearance"] == True)
    )

    category_ok = out["ranking_category_count"] >= config.min_category_count

    high_ok = (
        (out["from_recent_high_5m_pct"] <= config.max_from_recent_high_pct) &
        (out["from_recent_high_5m_pct"] >= config.min_from_recent_high_pct)
    )

    out["ranking_tonosama_price_ok"] = price_ok
    out["ranking_tonosama_volume_ok"] = volume_ok
    out["ranking_tonosama_rank_ok"] = rank_ok
    out["ranking_tonosama_high_ok"] = high_ok

    out["ranking_tonosama_ok"] = (
        price_ok &
        price_not_too_late &
        volume_ok &
        rank_ok &
        category_ok
    )

    out["ranking_tonosama_score"] = out["ranking_tonosama_raw_score"].fillna(0.0)

    return out