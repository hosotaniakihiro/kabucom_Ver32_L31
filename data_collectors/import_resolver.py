# ============================================================
# File   : data_collectors/import_resolver.py
# Version: DATA-COLLECTORS-IMPORT-RESOLVER-V1
# ------------------------------------------------------------
# Purpose:
#   - 既存プロジェクト側の関数名差分を吸収する
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Callable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

Candidate = Tuple[str, str]


def resolve_callable(candidates: Sequence[Candidate], *, required: bool = False) -> Optional[Callable]:
    """
    candidates:
        [
          ("module.path", "function_name"),
          ...
        ]
    """
    errors: list[str] = []

    for module_name, attr_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, attr_name)
            if callable(fn):
                logger.info("[IMPORT RESOLVER] resolved %s.%s", module_name, attr_name)
                return fn
            errors.append(f"{module_name}.{attr_name}: not callable")
        except Exception as e:
            errors.append(f"{module_name}.{attr_name}: {type(e).__name__}: {e}")

    logger.warning("[IMPORT RESOLVER] no callable resolved. candidates=%s errors=%s", candidates, errors)

    if required:
        raise ImportError("no callable resolved: " + " | ".join(errors))

    return None


def call_if_resolved(candidates: Sequence[Candidate], *args, required: bool = False, **kwargs):
    fn = resolve_callable(candidates, required=required)
    if fn is None:
        return None
    return fn(*args, **kwargs)
