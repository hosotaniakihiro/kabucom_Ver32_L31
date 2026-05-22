# ============================================================
# File   : core/startup/daily_signal_cache_fast_startup_patch.py
# Version: V1.0-SKIP-FULL-DAILY-CACHE-IN-MAIN
# ------------------------------------------------------------
# Purpose:
#   main.py 起動時の daily_signal_cache 全銘柄同期warmupをスキップし、
#   PUSH/ENTRY/EXIT の起動を優先する。
#
# Background:
#   startup_orchestrator._warmup_daily_signal_cache_safe() は
#   warmup_daily_signal_cache() を引数なしで呼ぶため、
#   stock_analysis_latest 全4192銘柄を同期ロードする。
#   ログ上は約8秒かかっている。
#
# Policy:
#   - main.py: 既定で全件warmupをスキップ
#   - main_database.py / data collectors: 従来どおり実行
#   - main.pyで強制したい場合: DAILY_SIGNAL_CACHE_RUN_IN_MAIN=1
#   - main.pyで非同期実行したい場合: DAILY_SIGNAL_CACHE_ASYNC_IN_MAIN=1
#
# Note:
#   候補DFに日足情報を付ける処理は daily_signal_cache 側の
#   fallback_load_if_empty により、必要時にロード可能。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL = None


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _is_database_process() -> bool:
    return any(
        _env_bool(name, False)
        for name in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        )
    )


def _should_skip_in_main() -> bool:
    if not _is_main_py_process():
        return False
    if _is_database_process():
        return False
    if _env_bool("DAILY_SIGNAL_CACHE_RUN_IN_MAIN", False):
        return False
    if _env_bool("DAILY_SIGNAL_CACHE_ASYNC_IN_MAIN", False):
        return False
    return _env_bool("DAILY_SIGNAL_CACHE_SKIP_IN_MAIN", True)


def _should_async_in_main() -> bool:
    if not _is_main_py_process():
        return False
    if _is_database_process():
        return False
    if _env_bool("DAILY_SIGNAL_CACHE_RUN_IN_MAIN", False):
        return False
    return _env_bool("DAILY_SIGNAL_CACHE_ASYNC_IN_MAIN", False)


def _set_global(name: str, value: Any) -> None:
    try:
        from global_state import global_data
        setattr(global_data, name, value)
    except Exception:
        pass


def _run_original_async(original) -> None:
    try:
        original()
    except Exception:
        logger.exception("[DAILY CACHE FAST STARTUP] async original warmup failed")


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True

    try:
        from core.startup import startup_orchestrator as orch
    except Exception as e:
        logger.warning("[DAILY CACHE FAST STARTUP] startup_orchestrator import failed err=%s", e, exc_info=False)
        return False

    original = getattr(orch, "_warmup_daily_signal_cache_safe", None)
    if not callable(original):
        logger.warning("[DAILY CACHE FAST STARTUP] target not found")
        return False

    _ORIGINAL = original

    def _patched_warmup_daily_signal_cache_safe() -> None:
        if _should_skip_in_main():
            _set_global("daily_signal_cache_startup_skipped", True)
            _set_global("daily_signal_cache_startup_async", False)
            logger.warning(
                "[DAILY CACHE FAST STARTUP] skipped full daily cache warmup in main.py. "
                "main_database.py handles full daily cache; set DAILY_SIGNAL_CACHE_RUN_IN_MAIN=1 to force."
            )
            return

        if _should_async_in_main():
            _set_global("daily_signal_cache_startup_skipped", False)
            _set_global("daily_signal_cache_startup_async", True)
            logger.warning("[DAILY CACHE FAST STARTUP] daily cache warmup started async in main.py")
            th = threading.Thread(
                target=_run_original_async,
                args=(original,),
                name="daily-signal-cache-fast-startup-async",
                daemon=True,
            )
            th.start()
            return

        return original()

    orch._warmup_daily_signal_cache_safe = _patched_warmup_daily_signal_cache_safe
    _INSTALLED = True
    logger.warning(
        "[DAILY CACHE FAST STARTUP] installed skip_in_main=%s async_in_main=%s run_in_main=%s argv_main=%s db_process=%s",
        _should_skip_in_main(),
        _should_async_in_main(),
        _env_bool("DAILY_SIGNAL_CACHE_RUN_IN_MAIN", False),
        _is_main_py_process(),
        _is_database_process(),
    )
    return True


try:
    install()
except Exception as e:
    logger.warning("[DAILY CACHE FAST STARTUP] auto install failed err=%s", e, exc_info=False)


__all__ = ["install"]
