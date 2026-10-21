# ============================================================
# File   : core/startup/push_symbol_bridge_modules/constants.py
# Version: PRODUCTION-STABLE-REV3.0
# ============================================================

from __future__ import annotations

from pathlib import Path
from nas_root import nas_path, nas_root

VERSION = "PRODUCTION-STABLE-REV3.0-PUSH-SYMBOL-BRIDGE-SPLIT-CANDIDATE100-REGISTER50-NAMELOG"

DEFAULT_MAX_SYMBOLS = 100
DEFAULT_REGISTER_LIMIT = 50

DEFAULT_SYMBOL_FLAGS_DB = Path(
    nas_path(r"Basic\symbol_flags.db")
)

DEFAULT_OPTIONAL_DB = Path(
    nas_path(r"raw_data\kabutan\optional_data.db")
)

__all__ = [
    "VERSION",
    "DEFAULT_MAX_SYMBOLS",
    "DEFAULT_REGISTER_LIMIT",
    "DEFAULT_SYMBOL_FLAGS_DB",
    "DEFAULT_OPTIONAL_DB",
]
