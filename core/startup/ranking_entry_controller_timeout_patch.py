# ============================================================
# File   : core/startup/ranking_entry_controller_timeout_patch.py
# Version: V1.0-RANKING-ENTRY-CONTROLLER-TIMEOUT
# ------------------------------------------------------------
# RANKING ENTRY は pending 作成後の entry_controller が20秒で
# timeoutしやすい。controller threadは裏で生き続けるため、
# scheduler側だけ timeout ログになり原因追跡が難しくなる。
#
# 対策:
#   - RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC を既定60秒へ引き上げる
#   - build timeout は既定90秒を維持し、必要なら env で上書き
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", "60")
        os.environ.setdefault("RANKING_ENTRY_BUILD_TIMEOUT_SEC", "90")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "90")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "300")

        import trading.entry_exit.tasks as tasks

        old_controller = float(getattr(tasks, "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 20.0) or 20.0)
        old_build = float(getattr(tasks, "RANKING_ENTRY_BUILD_TIMEOUT_SEC", 90.0) or 90.0)
        new_controller = max(old_controller, _env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 60.0), 60.0)
        new_build = max(old_build, _env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", 90.0), 90.0)

        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = new_controller
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = new_build
        _INSTALLED = True
        logger.warning(
            "[RANKING ENTRY TIMEOUT PATCH] installed controller_timeout %.1f->%.1f build_timeout %.1f->%.1f",
            old_controller,
            new_controller,
            old_build,
            new_build,
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY TIMEOUT PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY TIMEOUT PATCH] auto install failed")

__all__ = ["install"]
