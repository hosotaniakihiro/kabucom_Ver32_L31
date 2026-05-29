# ============================================================
# File   : core/startup/entry_direction_recursion_failopen_patch.py
# Version: Ver1.0-ENTRY-DIRECTION-RECURSION-FAILOPEN
# ------------------------------------------------------------
# 目的:
#   ENTRY_DIRECTION_CONFIRM の純粋ガード内で RecursionError が発生した時、
#   方向確認ガードだけを fail-open する。
#
# 背景:
#   high/low, ATR, LOW MOVE はOKなのに、以下で落ちるケースがある。
#     [ENTRY DIRECTION CONFIRM] recursion detected in pure guard. fail-safe NG.
#
# 方針:
#   - 既存の _direction_confirm() 本体はそのまま利用する。
#   - 通常NGはそのままNG。
#   - RecursionError の場合だけ True を返し、他の最終安全ガードへ進める。
#   - これにより、再帰バグだけで全エントリーを落とすことを防ぐ。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_CHECK = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def install() -> bool:
    global _INSTALLED, _ORIGINAL_CHECK
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_DIRECTION_RECURSION_FAILOPEN_ENABLED", True):
        logger.warning("[ENTRY DIRECTION RECURSION FAILOPEN] disabled by env")
        return False
    try:
        import core.startup.entry_direction_confirm_guard_patch as mod

        old = getattr(mod, "check_entry_direction_confirm", None)
        core_fn = getattr(mod, "_direction_confirm", None)
        if not callable(core_fn):
            logger.warning("[ENTRY DIRECTION RECURSION FAILOPEN] _direction_confirm not callable")
            return False
        if getattr(old, "_entry_direction_recursion_failopen", False):
            _INSTALLED = True
            return True

        _ORIGINAL_CHECK = old

        def _patched_check_entry_direction_confirm(entry_row: Any = None, *args, **kwargs) -> bool:
            if entry_row is None:
                return True
            try:
                return bool(core_fn(entry_row))
            except RecursionError:
                logger.warning(
                    "[ENTRY DIRECTION RECURSION FAILOPEN] recursion detected -> direction guard skipped only; other guards remain active",
                    exc_info=False,
                )
                return True
            except Exception as e:
                logger.warning(
                    "[ENTRY DIRECTION RECURSION FAILOPEN] direction guard failed -> keep old behavior NG err=%s",
                    e,
                    exc_info=False,
                )
                return False

        setattr(_patched_check_entry_direction_confirm, "_entry_direction_recursion_failopen", True)
        setattr(_patched_check_entry_direction_confirm, "_entry_direction_recursion_failopen_original", old)
        mod.check_entry_direction_confirm = _patched_check_entry_direction_confirm
        _INSTALLED = True
        logger.warning("[ENTRY DIRECTION RECURSION FAILOPEN] installed enabled=True")
        return True
    except Exception:
        logger.exception("[ENTRY DIRECTION RECURSION FAILOPEN] install failed")
        return False


__all__ = ["install"]
