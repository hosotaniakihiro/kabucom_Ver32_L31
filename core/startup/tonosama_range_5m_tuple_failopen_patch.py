# ============================================================
# File   : core/startup/tonosama_range_5m_tuple_failopen_patch.py
# Version: V1-TONOSAMA-RANGE5M-TUPLE-FAILOPEN
# ------------------------------------------------------------
# Purpose:
#   Tonosama entries can pass pending, AI, ATR and then stop at:
#     ENTRY_SKIP reason=RANGE_5M_FILTER_NG
#   even though RANGE_5M_FILTER_NG_FAIL_OPEN=1 is set.
#
# Cause:
#   entry_final_filter_failopen_patch currently fail-opens only when
#   range_5m_filter returns False. If it returns a tuple like
#     (False, {'reason': 'RANGE不足', ...})
#   that tuple is returned as-is and entry_controller treats it as NG.
#
# Fix:
#   For TONOSAMA only, fail-open tuple/False range NG. Other guards such as
#   LOW_MOVE, FINAL_ENTRY_SAFETY, board guard and order API still apply.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False
_ORIG = None


def _on(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return default
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return default


def _row_dict(v: Any) -> dict[str, Any]:
    try:
        if isinstance(v, dict):
            return v
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _is_tonosama(row: Any) -> bool:
    d = _row_dict(row)
    return str(d.get("source") or "").upper() == "TONOSAMA" or str(d.get("entry_type") or "").upper() == "TONOSAMA"


def _ret_ok(ret: Any) -> bool:
    try:
        if isinstance(ret, tuple) and len(ret) > 0:
            return bool(ret[0])
        return bool(ret)
    except Exception:
        return False


def _detail(ret: Any) -> Any:
    try:
        if isinstance(ret, tuple) and len(ret) > 1:
            return ret[1]
    except Exception:
        pass
    return None


def _apply() -> bool:
    global _INSTALLED, _ORIG
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.debug("[TONOSAMA RANGE5M TUPLE FAILOPEN] entry_controller not ready", exc_info=True)
        return False

    try:
        os.environ.setdefault("RANGE_5M_FILTER_NG_FAIL_OPEN", "1")
        os.environ.setdefault("RANGE_5M_FILTER_TONOSAMA_TUPLE_FAIL_OPEN", "1")
        cur = getattr(ec, "range_5m_filter", None)
        if not callable(cur):
            logger.warning("[TONOSAMA RANGE5M TUPLE FAILOPEN] target missing")
            return False
        if getattr(cur, "_tonosama_range5m_tuple_failopen_v1", False):
            _INSTALLED = True
            return True
        _ORIG = cur

        def _patched(entry_row: Any = None, *args: Any, **kwargs: Any):
            ret = _ORIG(entry_row, *args, **kwargs)
            try:
                if (
                    _is_tonosama(entry_row)
                    and _on("RANGE_5M_FILTER_TONOSAMA_TUPLE_FAIL_OPEN", True)
                    and not _ret_ok(ret)
                ):
                    logger.warning(
                        "[TONOSAMA RANGE5M TUPLE FAILOPEN] range_5m_filter NG -> fail-open symbol=%s ret=%s detail=%s",
                        _row_dict(entry_row).get("symbol"),
                        ret,
                        _detail(ret),
                    )
                    return True
            except Exception:
                logger.debug("[TONOSAMA RANGE5M TUPLE FAILOPEN] decision failed", exc_info=True)
            return ret

        _patched._tonosama_range5m_tuple_failopen_v1 = True  # type: ignore[attr-defined]
        _patched._original = cur  # type: ignore[attr-defined]
        ec.range_5m_filter = _patched
        _INSTALLED = True
        logger.warning("[TONOSAMA RANGE5M TUPLE FAILOPEN] installed v1")
        return True
    except Exception:
        logger.exception("[TONOSAMA RANGE5M TUPLE FAILOPEN] apply failed")
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLING
    if _apply():
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _loop() -> None:
            global _INSTALLING
            try:
                for _ in range(120):
                    if _apply():
                        return
                    time.sleep(0.25)
                logger.warning("[TONOSAMA RANGE5M TUPLE FAILOPEN] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_loop, name="tonosama-range5m-tuple-failopen", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA RANGE5M TUPLE FAILOPEN] auto install failed")


__all__ = ["install"]
