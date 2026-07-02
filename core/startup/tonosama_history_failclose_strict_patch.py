# ============================================================
# File   : core/startup/tonosama_history_failclose_strict_patch.py
# Version: V1-STRICT-TONOSAMA-HISTORY-FAILCLOSE
# ------------------------------------------------------------
# Purpose:
#   tonosama_history_missing_guard_patch.py の raw1履歴復旧ロジックは残しつつ、
#   履歴不足時の fail-open だけを最終的に fail-close へ戻す。
#
# Why:
#   ユーザー運用方針は「緩和しない」。
#   history_missing / surge history missing のまま entry を許可すると、
#   低出来高・低変動銘柄が通る可能性がある。
# ============================================================
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
VERSION = "V1-STRICT-TONOSAMA-HISTORY-FAILCLOSE"

_STRICT_VALUES = {
    "TONOSAMA_FORCE_HISTORY_FAILCLOSE": "1",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING": "0",
    "TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY": "0",
    "TONOSAMA_DROP_HISTORY_MISSING_ENTRY": "1",
    "TONOSAMA_HISTORY_MISSING_QUALITY_GUARD": "1",
    # raw1 / DB history recovery は許可。履歴を作れた時だけ判定に使う。
    "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
    "TONOSAMA_RAW1_HISTORY_RESAMPLE": "1",
    "TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED": "1",
}


def install() -> bool:
    changed: dict[str, tuple[str | None, str]] = {}
    try:
        for key, val in _STRICT_VALUES.items():
            old = os.environ.get(key)
            os.environ[key] = val
            if str(old) != str(val):
                changed[key] = (old, val)
        logger.warning(
            "[TONOSAMA HISTORY FAILCLOSE STRICT] installed version=%s changed=%s failopen=%s allow_without=%s allow_missing=%s drop_missing=%s raw1=%s push_raw_db=%s",
            VERSION,
            changed,
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            os.environ.get("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY"),
            os.environ.get("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY"),
            os.environ.get("TONOSAMA_DROP_HISTORY_MISSING_ENTRY"),
            os.environ.get("TONOSAMA_RAW1_HISTORY_RESAMPLE"),
            os.environ.get("TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA HISTORY FAILCLOSE STRICT] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA HISTORY FAILCLOSE STRICT] auto install failed")


__all__ = ["install", "VERSION"]
