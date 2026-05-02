# ============================================================
# File   : trading/summary/persistence/core/upsert_engine.py
# Version: Ver34.0-PRODUCTION-UPSERT-ENGINE-SPLIT-COMPAT
# ------------------------------------------------------------
# ✔ 旧 import 経路 compatibility
# ✔ 実体は upsert_executor / upsert_metadata / upsert_normalize / upsert_guards
# ✔ execute_upsert(df, interval) API 維持
# ✔ live DDL 禁止方針を維持
# ============================================================

from __future__ import annotations

from .upsert_executor import execute_upsert

__all__ = [
    "execute_upsert",
]