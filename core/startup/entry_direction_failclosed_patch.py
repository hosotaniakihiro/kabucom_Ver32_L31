# ============================================================
# File   : core/startup/entry_direction_failclosed_patch.py
# Version: V1.0-DIRECTION-CONFIRM-FAIL-CLOSED
# ------------------------------------------------------------
# 【目的】
#   entry_direction_confirm の RecursionError / 例外時に fail-open しない。
#   方向確認が壊れている場合は安全側NGでエントリー停止する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    os.environ["ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN"] = "0"
    os.environ["ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN"] = "0"

    try:
        import core.startup.entry_direction_confirm_guard_patch as dgp
    except Exception:
        logger.exception("[ENTRY DIRECTION FAILCLOSED] import failed")
        return False

    orig = getattr(dgp, "check_entry_direction_confirm", None)
    if not callable(orig):
        logger.warning("[ENTRY DIRECTION FAILCLOSED] target not callable")
        return False

    if getattr(orig, "_entry_direction_failclosed_v1", False):
        _PATCHED = True
        return True

    def _check_entry_direction_confirm_failclosed(entry_row: Any = None, *args, **kwargs) -> bool:
        try:
            return bool(orig(entry_row, *args, **kwargs))
        except RecursionError:
            logger.error("[ENTRY DIRECTION FAILCLOSED] recursion detected -> NG", exc_info=False)
            return False
        except Exception as e:
            logger.warning("[ENTRY DIRECTION FAILCLOSED] error -> NG err=%s", e, exc_info=False)
            return False

    _check_entry_direction_confirm_failclosed._entry_direction_failclosed_v1 = True  # type: ignore[attr-defined]
    _check_entry_direction_confirm_failclosed._original = orig  # type: ignore[attr-defined]
    dgp.check_entry_direction_confirm = _check_entry_direction_confirm_failclosed

    _PATCHED = True
    logger.warning(
        "[ENTRY DIRECTION FAILCLOSED] installed recursion_fail_open=%s error_fail_open=%s",
        os.getenv("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN"),
        os.getenv("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY DIRECTION FAILCLOSED] auto install failed")

__all__ = ["install"]
