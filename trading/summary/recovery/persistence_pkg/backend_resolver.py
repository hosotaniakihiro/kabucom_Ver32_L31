# ============================================================
# File   : trading/summary/recovery/persistence_pkg/backend_resolver.py
# Ver    : PRODUCTION-STABLE-REV9.0-BACKEND-RESOLVER
# ------------------------------------------------------------
# 【概要】
#   execute_upsert / save_summary_bulk backend resolver
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def resolve_callable(candidates: list[tuple[str, str]]) -> tuple[Optional[Callable], Optional[str]]:
    for mod_name, fn_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                backend_name = f"{mod_name}.{fn_name}"
                logger.info("[summary.recovery.persistence] resolved backend %s", backend_name)
                return fn, backend_name
        except Exception:
            logger.debug(
                "[summary.recovery.persistence] backend resolve failed mod=%s fn=%s",
                mod_name,
                fn_name,
                exc_info=True,
            )
    return None, None


execute_upsert_backend, execute_upsert_backend_name = resolve_callable(
    [
        ("trading.summary.persistence.upsert_engine", "execute_upsert"),
        ("trading.summary.persistence.core.upsert_engine", "execute_upsert"),
        ("trading.summary.persistence.core.upsert_executor", "execute_upsert"),
        ("trading.summary.persistence.core.upsert_executor", "run_upsert"),
    ]
)

save_summary_bulk_backend, save_summary_bulk_backend_name = resolve_callable(
    [
        ("trading.summary.persistence.summary_saver_bulk", "save_summary_bulk"),
        ("trading.summary.persistence.summary_saver_bulk", "save_summary_df"),
        ("trading.summary.persistence.bulk.summary_saver_bulk", "save_summary_bulk"),
    ]
)

__all__ = [
    "resolve_callable",
    "execute_upsert_backend",
    "execute_upsert_backend_name",
    "save_summary_bulk_backend",
    "save_summary_bulk_backend_name",
]