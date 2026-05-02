# ============================================================
# File   : core/startup/push_symbol_bridge.py
# Version: PRODUCTION-STABLE-REV3.0-COMPAT-WRAPPER
# ------------------------------------------------------------
# Purpose:
#   既存import互換を維持する薄いラッパー。
#
#   既存:
#     from core.startup.push_symbol_bridge import install_real_push_symbols
#
#   は変更不要。
#
# Implementation:
#   実処理は core/startup/push_symbol_bridge_modules/ 配下へ分割。
# ============================================================

from __future__ import annotations

from .push_symbol_bridge_modules.normalize import (
    clean_symbols,
)
from .push_symbol_bridge_modules.rotation import (
    DEFAULT_MAX_SYMBOLS,
    DEFAULT_REGISTER_LIMIT,
    split_register_rotation,
)
from .push_symbol_bridge_modules.providers import (
    resolve_real_push_symbols,
)
from .push_symbol_bridge_modules.service import (
    VERSION,
    install_real_push_symbols,
)

__all__ = [
    "VERSION",
    "DEFAULT_MAX_SYMBOLS",
    "DEFAULT_REGISTER_LIMIT",
    "clean_symbols",
    "split_register_rotation",
    "resolve_real_push_symbols",
    "install_real_push_symbols",
]
