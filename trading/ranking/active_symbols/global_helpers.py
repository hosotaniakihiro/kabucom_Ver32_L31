# ============================================================
# File   : trading/ranking/active_symbols/global_helpers.py
# Version: Ver1.0-ACTIVE-SYMBOLS-GLOBAL-HELPERS
# ============================================================
from __future__ import annotations
from typing import Any
from global_state import global_data


def set_global_attr(name: str, value: Any) -> None:
    try:
        setattr(global_data, name, value)
    except Exception:
        pass


def get_global_attr(name: str, default: Any = None) -> Any:
    try:
        return getattr(global_data, name, default)
    except Exception:
        return default
