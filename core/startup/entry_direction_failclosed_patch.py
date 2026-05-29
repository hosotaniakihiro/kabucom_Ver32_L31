# ============================================================
# File   : core/startup/entry_direction_failclosed_patch.py
# Version: V1.1-DIRECTION-CONFIRM-RECURSION-OPTIONAL-FAILOPEN
# ------------------------------------------------------------
# 【目的】
#   entry_direction_confirm の通常例外は安全側NGにする。
#   ただし RecursionError は wrapper衝突由来のため、環境変数で fail-open 可能にする。
#
# 既定:
#   ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN=1
#   ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN=0
#
# これにより、方向確認の再帰だけで全候補を落とさず、
# LOW MOVE / liquidity / credit / board 等の他ガードはそのまま残す。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
    except Exception:
        pass


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    _setdefault_env("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN", "1")
    _setdefault_env("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN", "0")

    try:
        import core.startup.entry_direction_confirm_guard_patch as dgp
    except Exception:
        logger.exception("[ENTRY DIRECTION FAILCLOSED] import failed")
        return False

    orig = getattr(dgp, "check_entry_direction_confirm", None)
    if not callable(orig):
        logger.warning("[ENTRY DIRECTION FAILCLOSED] target not callable")
        return False

    if getattr(orig, "_entry_direction_failclosed_v11", False):
        _PATCHED = True
        return True

    def _check_entry_direction_confirm_guarded(entry_row: Any = None, *args, **kwargs) -> bool:
        try:
            return bool(orig(entry_row, *args, **kwargs))
        except RecursionError:
            allow = _env_bool("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN", True)
            logger.warning(
                "[ENTRY DIRECTION FAILCLOSED] recursion detected -> fail_open=%s; other guards remain active",
                allow,
                exc_info=False,
            )
            return bool(allow)
        except Exception as e:
            allow = _env_bool("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN", False)
            logger.warning("[ENTRY DIRECTION FAILCLOSED] error -> fail_open=%s err=%s", allow, e, exc_info=False)
            return bool(allow)

    _check_entry_direction_confirm_guarded._entry_direction_failclosed_v11 = True  # type: ignore[attr-defined]
    _check_entry_direction_confirm_guarded._original = orig  # type: ignore[attr-defined]
    dgp.check_entry_direction_confirm = _check_entry_direction_confirm_guarded

    _PATCHED = True
    logger.warning(
        "[ENTRY DIRECTION FAILCLOSED] installed v1.1 recursion_fail_open=%s error_fail_open=%s",
        os.getenv("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN"),
        os.getenv("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY DIRECTION FAILCLOSED] auto install failed")


__all__ = ["install"]
