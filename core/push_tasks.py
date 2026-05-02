# ============================================================
# File   : core/push_tasks.py
# Version: PRODUCTION-STABLE-REV1.0-COMPAT-SHIM
# ------------------------------------------------------------
# 【概要】
#   旧 import パス:
#       core.push_tasks.register_push_tasks
#   を維持するための互換 shim
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _resolve_callable(candidates: list[tuple[str, str]]) -> Optional[Callable[..., Any]]:
    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info("[core.push_tasks] resolved %s.%s", module_name, func_name)
                return fn
        except Exception:
            logger.debug("[core.push_tasks] import failed %s.%s", module_name, func_name, exc_info=True)
    return None


def register_push_tasks(*args: Any, **kwargs: Any) -> bool:
    candidates = [
        ("core.scheduler_tasks", "register_summary_only_tasks"),
        ("scheduler_jobs.summary.scheduler", "register_push_summary_tasks"),
        ("scheduler_jobs.summary.scheduler", "register_summary_tasks"),
    ]

    fn = _resolve_callable(candidates)
    if fn is None:
        logger.warning("[core.push_tasks] no backend register_push_tasks found -> no-op")
        return False

    try:
        result = fn(*args, **kwargs)
        return bool(result) if result is not None else True
    except Exception:
        logger.exception("[core.push_tasks] register_push_tasks failed")
        return False