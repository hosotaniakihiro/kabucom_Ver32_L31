# ============================================================
# File   : core/startup/push_symbol_bridge_modules/__init__.py
# Version: PRODUCTION-STABLE-REV3.0
# ------------------------------------------------------------
# Purpose:
#   push_symbol_bridge 分割モジュール公開API
# ============================================================

from __future__ import annotations

from .normalize import clean_symbols
from .rotation import (
    DEFAULT_MAX_SYMBOLS,
    DEFAULT_REGISTER_LIMIT,
    split_register_rotation,
    select_register_symbols,
)
from .providers import resolve_real_push_symbols
from .service import VERSION, install_real_push_symbols

__all__ = [
    "VERSION",
    "DEFAULT_MAX_SYMBOLS",
    "DEFAULT_REGISTER_LIMIT",
    "clean_symbols",
    "split_register_rotation",
    "select_register_symbols",
    "resolve_real_push_symbols",
    "install_real_push_symbols",
]
