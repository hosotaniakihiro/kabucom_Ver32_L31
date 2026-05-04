# ============================================================
# File   : trading/ranking/active_symbols/protected.py
# Version: Ver1.0-ACTIVE-SYMBOLS-PROTECTED
# ============================================================
from __future__ import annotations
import logging
from typing import Set
from global_state import global_data
from .normalize import normalize_symbol

logger = logging.getLogger(__name__)


def get_protected_symbols() -> Set[str]:
    protected: Set[str] = set()
    try:
        if hasattr(global_data, "open_positions"):
            for p in global_data.open_positions:
                s = p.get("symbol") if isinstance(p, dict) else p
                ns = normalize_symbol(s)
                if ns:
                    protected.add(ns)
    except Exception:
        logger.debug("[ACTIVE] failed to read open_positions", exc_info=True)
    try:
        if hasattr(global_data, "pending_entries"):
            for p in global_data.pending_entries:
                s = p.get("symbol") if isinstance(p, dict) else p
                ns = normalize_symbol(s)
                if ns:
                    protected.add(ns)
    except Exception:
        logger.debug("[ACTIVE] failed to read pending_entries", exc_info=True)
    return protected
