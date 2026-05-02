# ============================================================
# File   : trading/push/allocator/config.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-CONFIG
# ------------------------------------------------------------
# ✔ push slot allocator config
# ✔ 50 symbol limit support
# ✔ churn prevention config
# ✔ priority weights
# ✔ ETF filter
# ✔ runtime safe defaults
# ✔ production ready
# ============================================================

from __future__ import annotations

from dataclasses import dataclass


# ============================================================
# allocator limits
# ============================================================

MAX_PUSH_SYMBOLS: int = 50


# ============================================================
# symbol tier sizes
# ============================================================

ACTIVE_TARGET: int = 20
LIGHT_TARGET: int = 15
RANKING_TARGET: int = 10
OPENING_TARGET: int = 5


# ============================================================
# churn prevention
# ============================================================

MIN_HOLD_SECONDS: int = 120
HYSTERESIS_SCORE_MARGIN: float = 0.05


# ============================================================
# ETF filter
# ============================================================

ETF_SYMBOL_PREFIX = ()


# ============================================================
# priority weights
# ============================================================

WEIGHT_POSITION: float = 1000.0
WEIGHT_ORDER_PENDING: float = 800.0
WEIGHT_ACTIVE: float = 200.0
WEIGHT_LIGHT: float = 100.0
WEIGHT_RANKING: float = 80.0
WEIGHT_OPENING: float = 60.0


# ============================================================
# ranking score weights
# ============================================================

RANK_SCORE_MULTIPLIER: float = 100.0


# ============================================================
# slot allocator config dataclass
# ============================================================

@dataclass
class AllocatorConfig:

    max_push_symbols: int = MAX_PUSH_SYMBOLS

    active_target: int = ACTIVE_TARGET
    light_target: int = LIGHT_TARGET
    ranking_target: int = RANKING_TARGET
    opening_target: int = OPENING_TARGET

    min_hold_seconds: int = MIN_HOLD_SECONDS
    hysteresis_margin: float = HYSTERESIS_SCORE_MARGIN

    weight_position: float = WEIGHT_POSITION
    weight_order_pending: float = WEIGHT_ORDER_PENDING
    weight_active: float = WEIGHT_ACTIVE
    weight_light: float = WEIGHT_LIGHT
    weight_ranking: float = WEIGHT_RANKING
    weight_opening: float = WEIGHT_OPENING

    rank_score_multiplier: float = RANK_SCORE_MULTIPLIER

    etf_prefix = ETF_SYMBOL_PREFIX


# ============================================================
# default config
# ============================================================

DEFAULT_ALLOCATOR_CONFIG = AllocatorConfig()