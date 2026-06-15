# ============================================================
# File   : trading/ranking/active_symbols/__init__.py
# Version: Ver1.1-ACTIVE-SYMBOLS-PACKAGE-PREMARKET-GETTER-PATCH
# ============================================================
from __future__ import annotations

from .manager import (
    update_active_symbols,
    get_active_symbols,
    get_current_active_symbols,
    get_monitor_symbols,
    get_push_symbols,
    get_register_symbols,
    get_subscription_symbols,
    get_rotation_symbols,
    debug_active_symbols,
)
from .ranking_source import (
    update_last_seen_from_ranking,
    extract_volume_speed_symbols,
    build_liquidity_map,
)
from .liquidity import filter_liquid_symbols, is_liquid_symbol
from .symbol_flags import load_symbol_flags_eligible_symbols

# Passive getter calls after a premarket update must not re-run stricter
# non-premarket price guarding and collapse 100 SBI symbols to zero.
try:
    from .getter_premarket_patch import install as _install_getter_premarket_patch

    _install_getter_premarket_patch()
    from .manager import (  # re-export patched callables
        get_active_symbols,
        get_current_active_symbols,
        get_monitor_symbols,
        get_push_symbols,
        get_register_symbols,
        get_subscription_symbols,
        get_rotation_symbols,
    )
except Exception:
    pass

__all__ = [
    "update_active_symbols",
    "update_last_seen_from_ranking",
    "extract_volume_speed_symbols",
    "build_liquidity_map",
    "filter_liquid_symbols",
    "is_liquid_symbol",
    "load_symbol_flags_eligible_symbols",
    "get_active_symbols",
    "get_current_active_symbols",
    "get_monitor_symbols",
    "get_push_symbols",
    "get_register_symbols",
    "get_subscription_symbols",
    "get_rotation_symbols",
    "debug_active_symbols",
]
