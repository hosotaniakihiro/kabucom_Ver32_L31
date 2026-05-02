# ============================================================
# File   : trading/summary/recovery/persistence_pkg/imports.py
# Ver    : PRODUCTION-STABLE-REV9.0-PERSISTENCE-IMPORTS
# ------------------------------------------------------------
# 【概要】
#   optional imports / global_data compatibility
# ============================================================

from __future__ import annotations

try:
    from global_state import global_data
except Exception:  # pragma: no cover
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None

__all__ = [
    "global_data",
]