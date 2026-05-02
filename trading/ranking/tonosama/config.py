# ============================================================
# File   : trading/ranking/tonosama/config.py
# Purpose: ランキング主導 殿様イナゴ検知 設定
# ============================================================

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RankingTonosamaConfig:
    lookback_minutes: int = 30

    # 価格
    min_price_change_1m_pct: float = 0.8
    min_price_change_3m_pct: float = 1.5
    max_price_change_5m_pct: float = 12.0

    # 出来高
    min_volume_delta_1m: float = 1000.0
    min_volume_spike_ratio: float = 2.0

    # ランキング
    rank_up_threshold: int = 10
    max_rank: int = 50
    min_category_count: int = 1

    # 過熱除外
    max_from_recent_high_pct: float = -0.1
    min_from_recent_high_pct: float = -7.0

    # 出力
    max_candidates: int = 10


DEFAULT_RANKING_TONOSAMA_CONFIG = RankingTonosamaConfig()