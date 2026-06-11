from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
_DONE = False
_OPTIONALS_DONE = False


def _env_true(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if raw in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    except Exception:
        pass
    return bool(default)


def _install_optional(label: str, module_name: str) -> bool:
    try:
        mod = __import__(module_name, fromlist=["install"])
        fn = getattr(mod, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[SUMMARY AI NO DIRECT SYNC] companion %s installed=%s", label, ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI NO DIRECT SYNC] companion %s install failed", label)
        return False


def _apply_env_only() -> bool:
    os.environ["SUMMARY_AI_ASYNC_ENTRY"] = "1"
    os.environ["SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"] = "1"
    os.environ["SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC"] = "1"
    os.environ["SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS"] = os.getenv("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", "2")
    os.environ["SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC"] = os.getenv("SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC", "0.35")
    os.environ["SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY"] = "0"
    os.environ["SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX"] = os.getenv("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", "3")
    os.environ["SUMMARY_AI_ASYNC_ENTRY_STALE_SEC"] = os.getenv("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", "90")
    os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY"] = "1"
    os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX"] = os.getenv("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", "8")
    os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC"] = os.getenv("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", "2.0")

    # main.py 側の summary parent tick は戻す。ただしCPU高止まり防止のため、
    # 1m/3m/5m全足を常時強制しない。必要なら環境変数で明示的に戻す。
    os.environ["AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK"] = "0"
    os.environ["FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK"] = "1"
    os.environ["ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED"] = "1"

    os.environ.setdefault("SUMMARY_PUSH_BG_ALL_INTERVALS", "0")
    os.environ.setdefault("SUMMARY_PUSH_BG_LONG_INTERVALS", "0")
    os.environ.setdefault("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS", "0")
    os.environ.setdefault("SUMMARY_PARALLEL_FORCE_1_3_5", "0")
    os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_WORKERS", "1")
    os.environ.setdefault("SUMMARY_PUSH_BG_INTERVAL_WORKERS", "1")

    os.environ["ENTRY_ORDER_NO5S_FALLBACK_ENABLED"] = "1"
    os.environ["ENTRY_ORDER_NO5S_FALLBACK_SOURCES"] = "RANKING,TONOSAMA"
    os.environ["ENTRY_BOARD_IMBALANCE_ADVISORY_ENABLED"] = "1"
    return True


def apply_patch(*, install_optionals: bool = True) -> bool:
    global _OPTIONALS_DONE
    ok = _apply_env_only()

    # 重要: watcherから毎秒 companion install を繰り返さない。
    # 各 companion は watcher/threadを持つものがあり、再install連発でCPUとログが肥大化する。
    if install_optionals and not _OPTIONALS_DONE:
        _install_optional("SUMMARY_DIRECT_DISPATCH", "core.startup.summary_ai_async_direct_dispatch_patch")
        _install_optional("FINAL_BOARD_SIGNATURE_LAST", "core.startup.final_entry_board_guard_signature_last_patch")
        _install_optional("ENTRY_ORDER_NO5S_FALLBACK", "core.startup.entry_order_no5s_fallback_patch")
        _install_optional("BOARD_IMBALANCE_ADVISORY", "core.startup.entry_board_imbalance_advisory_patch")
        _OPTIONALS_DONE = True
    return ok


def watch() -> None:
    if not _env_true("SUMMARY_AI_NO_DIRECT_SYNC_WATCHER_ENABLED", default=True):
        logger.warning("[SUMMARY AI NO DIRECT SYNC] watcher disabled by env")
        return

    loops = max(1, min(int(float(os.getenv("SUMMARY_AI_NO_DIRECT_SYNC_WATCH_LOOPS", "12") or 12)), 60))
    sleep_sec = max(2.0, min(float(os.getenv("SUMMARY_AI_NO_DIRECT_SYNC_WATCH_SLEEP_SEC", "5.0") or 5.0), 30.0))
    for i in range(loops):
        ok = apply_patch(install_optionals=False)
        if i in (0, loops - 1):
            logger.warning(
                "[SUMMARY AI NO DIRECT SYNC] enforce v6 i=%s/%s ok=%s direct_sync=%s direct_dispatch=%s parent_skip=%s force_parent=%s no5s=%s board_adv=%s force_1_3_5=%s workers=%s",
                i,
                loops,
                ok,
                os.environ.get("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"),
                os.environ.get("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC"),
                os.environ.get("AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK"),
                os.environ.get("FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK"),
                os.environ.get("ENTRY_ORDER_NO5S_FALLBACK_ENABLED"),
                os.environ.get("ENTRY_BOARD_IMBALANCE_ADVISORY_ENABLED"),
                os.environ.get("SUMMARY_PARALLEL_FORCE_1_3_5"),
                os.environ.get("SUMMARY_PARALLEL_INTERVAL_WORKERS"),
            )
        time.sleep(sleep_sec)


def install() -> bool:
    global _DONE
    if _DONE:
        return apply_patch(install_optionals=False)

    ok = apply_patch(install_optionals=True)
    _DONE = True

    if _env_true("SUMMARY_AI_NO_DIRECT_SYNC_WATCHER_ENABLED", default=True):
        threading.Thread(target=watch, name="summary-ai-direct-sync-compat", daemon=True).start()
        watcher = True
    else:
        watcher = False

    logger.warning(
        "[SUMMARY AI NO DIRECT SYNC] installed v6 ok=%s watcher=%s direct_sync=%s direct_dispatch=%s parent_skip=%s force_parent=%s no5s=%s board_adv=%s force_1_3_5=%s workers=%s",
        ok,
        watcher,
        os.environ.get("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"),
        os.environ.get("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC"),
        os.environ.get("AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK"),
        os.environ.get("FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK"),
        os.environ.get("ENTRY_ORDER_NO5S_FALLBACK_ENABLED"),
        os.environ.get("ENTRY_BOARD_IMBALANCE_ADVISORY_ENABLED"),
        os.environ.get("SUMMARY_PARALLEL_FORCE_1_3_5"),
        os.environ.get("SUMMARY_PARALLEL_INTERVAL_WORKERS"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI NO DIRECT SYNC] auto install failed")

__all__ = ["install"]
