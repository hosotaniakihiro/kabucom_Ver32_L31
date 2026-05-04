# ============================================================
# File   : trading/ranking/active_symbols/reflect.py
# Version: Ver1.0-ACTIVE-SYMBOLS-REFLECT
# ============================================================
from __future__ import annotations
from typing import Iterable
from global_state import global_data
from .global_helpers import set_global_attr
from .normalize import dedupe_keep_order


def reflect_active_to_global(active: Iterable[str]) -> None:
    ordered = dedupe_keep_order(active)
    global_data.symbols_active = set(ordered)
    set_global_attr("active_symbols", ordered)
    set_global_attr("monitor_symbols", ordered)
    set_global_attr("candidate_push_symbols", ordered)
    set_global_attr("push_candidate_symbols", ordered)
    set_global_attr("push_symbols_100", ordered)
    set_global_attr("push_symbols", ordered[:50])
    set_global_attr("register_symbols", ordered[:50])
    set_global_attr("subscription_symbols", ordered[:50])
    set_global_attr("ats_register_targets", ordered[:50])
    set_global_attr("ats_targets", ordered[:50])
