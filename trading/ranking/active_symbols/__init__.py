# ============================================================
# File   : trading/ranking/active_symbols/__init__.py
# Version: Ver1.2-ACTIVE-SYMBOLS-DB-RANKING-FALLBACK
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

# getter_premarket_patch と db_ranking_fallback_patch は manager.py 本体へ
# 本文化した。
#   - 寄前 SBI 由来リストの扱い -> manager._get_active_symbols_premarket()
#   - today_ranking 0 件時の DB 復元 -> manager.get_active_symbols /
#     update_active_symbols (データ取得部は db_ranking_fallback.py)
# 合成順 (db fallback <- premarket <- native) はそのまま。
# install が無くなったので上の from .manager import が最終形になり、
# 再 import も不要になった。

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
