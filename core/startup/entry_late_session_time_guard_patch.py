# ============================================================
# File   : core/startup/entry_late_session_time_guard_patch.py
# Version: V1-LATE-SESSION-ENTRY-1520
# ------------------------------------------------------------
# 目的:
#   final_entry_safety_guard_patch のデフォルト ENTRY_NO_NEW_AFTER=14:55 により、
#   Summary AI が 14:55 以降に承認した候補が発注直前で time_after_allowed に
#   なり、ORDER_BUILD_OK / ENTRY_DISPATCH まで到達しない問題を修正する。
#
# 方針:
#   - 新規エントリー許可を 15:20 まで延長する。
#   - 15:20 以降は従来どおり新規停止し、引け前の無理な新規を避ける。
# ============================================================
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def install() -> bool:
    global _INSTALLED
    try:
        old = os.getenv("ENTRY_NO_NEW_AFTER")
        new = os.getenv("ENTRY_LATE_SESSION_NO_NEW_AFTER", "15:20")
        if _env_bool("FORCE_ENTRY_NO_NEW_AFTER", True):
            os.environ["ENTRY_NO_NEW_AFTER"] = new
        else:
            os.environ.setdefault("ENTRY_NO_NEW_AFTER", new)
        _INSTALLED = True
        logger.warning(
            "[ENTRY LATE SESSION TIME GUARD] installed old_no_new_after=%s new_no_new_after=%s force=%s",
            old,
            os.environ.get("ENTRY_NO_NEW_AFTER"),
            _env_bool("FORCE_ENTRY_NO_NEW_AFTER", True),
        )
        return True
    except Exception:
        logger.exception("[ENTRY LATE SESSION TIME GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY LATE SESSION TIME GUARD] auto install failed")

__all__ = ["install"]
