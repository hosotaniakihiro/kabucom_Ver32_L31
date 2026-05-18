from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _is_positive_order_result_strict(result: Any) -> bool:
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return bool(result)
        if isinstance(result, dict):
            # approved / entries は「候補があった」だけで、発注成功ではない。
            for key in ("executed", "order_sent", "order_submitted", "success", "entry_executed"):
                if result.get(key) is True:
                    return True
            for key in ("order_id", "OrderId", "orders", "order_ids", "sent_orders"):
                v = result.get(key)
                if isinstance(v, (list, tuple, set, dict)):
                    if len(v) > 0:
                        return True
                elif v:
                    return True
            nested = result.get("pipeline_result") or result.get("result")
            if nested is not None and nested is not result:
                return _is_positive_order_result_strict(nested)
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_is_positive_order_result_strict(x) for x in result)
        return bool(result)
    except Exception:
        logger.exception("[SUMMARY AI RESULT PATCH] judgement failed result=%s", result)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from trading.entry.summary_ai import executor
        old_fn = getattr(executor, "_is_positive_order_result", None)
        if callable(old_fn) and getattr(old_fn, "_strict_result_patch_v1", False):
            _INSTALLED = True
            return True
        _is_positive_order_result_strict._strict_result_patch_v1 = True  # type: ignore[attr-defined]
        _is_positive_order_result_strict._original = old_fn  # type: ignore[attr-defined]
        executor._is_positive_order_result = _is_positive_order_result_strict
        _INSTALLED = True
        logger.warning("[SUMMARY AI RESULT PATCH] installed strict executed judgement")
        return True
    except Exception:
        logger.exception("[SUMMARY AI RESULT PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RESULT PATCH] auto install failed")

__all__ = ["install"]
