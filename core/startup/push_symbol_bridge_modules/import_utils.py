# ============================================================
# File   : core/startup/push_symbol_bridge_modules/import_utils.py
# Version: PRODUCTION-STABLE-REV3.0
# ------------------------------------------------------------
# Purpose:
#   import / callable helper
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable

from .constants import DEFAULT_MAX_SYMBOLS

logger = logging.getLogger(__name__)


def import_attr(module_name: str, attr_name: str) -> Any:
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, attr_name, None)
    except Exception:
        return None


def safe_call(fn: Callable[..., Any], *, limit: int = DEFAULT_MAX_SYMBOLS) -> Any:
    call_patterns = (
        lambda: fn(limit=limit),
        lambda: fn(max_symbols=limit),
        lambda: fn(n=limit),
        lambda: fn(limit),
        lambda: fn(),
    )

    for caller in call_patterns:
        try:
            return caller()
        except TypeError:
            continue
        except Exception:
            logger.debug(
                "[PUSH SYMBOL BRIDGE] provider call failed fn=%s",
                fn,
                exc_info=True,
            )
            return None

    return None


def get_global_data() -> Any:
    candidates = (
        ("global_state", "global_data"),
        ("core.global_context.context", "global_data"),
    )

    for module_name, attr_name in candidates:
        gd = import_attr(module_name, attr_name)
        if gd is not None:
            return gd

    return None


__all__ = [
    "import_attr",
    "safe_call",
    "get_global_data",
]
