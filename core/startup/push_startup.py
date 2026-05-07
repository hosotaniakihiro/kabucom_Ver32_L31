# ============================================================
# File   : core/startup/push_startup.py
# Version: FINAL-PRODUCTION-REV23.2-PUSH-STARTUP-FULL-SPLIT-MODE
# ------------------------------------------------------------
# 【概要】
#   PUSH 関連の起動処理を担当
#
# Split mode:
#   - main_database.py が PUSH保存 / PUSH受信 / 銘柄登録 / PUSH DB準備を担当する
#   - main.py 側では PUSH DB読込 bootstrap / PUSH writer / PUSH stream / symbol bridge を起動しない
#
# 理由:
#   - push_bootstrap / symbol_bootstrap / push_symbol_bridge 経由で
#     database.session.init_engines() が走り、main.py 側で push/ranking DB が作成されるため
#   - 完全に main_database.py へ切り離すには、main.py側の PUSH stack 全体を skip する必要がある
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Optional

from global_state import global_data

from core.startup.push_bootstrap import bootstrap_push
from core.startup.push_storage_bootstrap import (
    start_push_storage,
    is_push_storage_running,
)
from core.startup.symbol_bootstrap import bootstrap_symbols

from core.startup.startup_config import (
    PUSH_DIR,
    resolve_attr,
    count_symbol_quality,
    head,
)

logger = logging.getLogger(__name__)

VERSION = "FINAL-PRODUCTION-REV23.2-PUSH-STARTUP-FULL-SPLIT-MODE"


def _should_skip_push_collection_in_main() -> bool:
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


# ============================================================
# push storage
# ============================================================

def start_push_storage_safe() -> None:
    if _should_skip_push_collection_in_main():
        logger.warning(
            "📡 push storage bootstrap skipped in main process because "
            "AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1; main_database.py handles PUSH storage."
        )
        try:
            global_data.push_writer_running = False
            global_data.push_storage_running = False
            global_data.push_storage_skipped_external = True
        except Exception:
            pass
        return

    try:
        logger.info("📡 push storage bootstrap start")
        start_push_storage(buffer_size=100)

        running = False
        try:
            running = bool(is_push_storage_running())
        except Exception:
            logger.debug("[push_startup] push storage running check failed", exc_info=True)
            running = False

        try:
            global_data.push_writer_running = bool(running)
            global_data.push_storage_running = bool(running)
            global_data.push_storage_skipped_external = False
        except Exception:
            pass

        logger.info("✅ push storage bootstrap complete running=%s", running)

        if not running:
            logger.warning(
                "⚠ push storage bootstrap completed but writer is not running. "
                "Check core.startup.push_storage_bootstrap and push_db_writer logs."
            )
    except Exception:
        try:
            global_data.push_writer_running = False
            global_data.push_storage_running = False
        except Exception:
            pass
        logger.exception("❌ push storage bootstrap failed")


def bootstrap_push_safe() -> None:
    if _should_skip_push_collection_in_main():
        logger.warning(
            "📡 push bootstrap skipped in main process because "
            "main_database.py handles PUSH DB read/write collection."
        )
        try:
            global_data.push_bootstrap_skipped_external = True
        except Exception:
            pass
        return

    try:
        bootstrap_push(PUSH_DIR)
    except Exception:
        logger.exception("❌ Push bootstrap failed")


def bootstrap_symbols_safe() -> None:
    if _should_skip_push_collection_in_main():
        logger.warning(
            "🔖 symbol bootstrap skipped from PUSH stack in main process because "
            "main_database.py handles collector-side symbol registration."
        )
        try:
            global_data.push_symbol_bootstrap_skipped_external = True
        except Exception:
            pass
        return

    try:
        bootstrap_symbols()
    except Exception:
        logger.exception("❌ Symbol bootstrap failed")
        raise


# ============================================================
# push symbol bridge
# ============================================================

def inspect_global_push_symbols(label: str) -> None:
    attrs = (
        "monitor_symbols",
        "active_symbols",
        "push_symbols",
        "register_symbols",
        "ats_register_targets",
        "ats_targets",
    )

    for attr in attrs:
        try:
            val = getattr(global_data, attr, None)
        except Exception:
            val = None

        if val is None:
            continue

        raw, real, filler = count_symbol_quality(val)

        logger.info(
            "[push_startup][PUSH SYMBOL INSPECT] %s global_data.%s raw=%d real=%d filler=%d head=%s",
            label,
            attr,
            raw,
            real,
            filler,
            head(val, 10),
        )


def install_real_push_symbols_safe() -> list[str]:
    if _should_skip_push_collection_in_main():
        logger.warning(
            "🔗 push real-symbol bridge skipped in main process because "
            "main_database.py handles PUSH registration targets."
        )
        try:
            global_data.push_symbol_bridge_installed = False
            global_data.push_symbol_bridge_count = 0
            global_data.push_symbol_bridge_symbols = []
            global_data.push_symbol_bridge_skipped_external = True
        except Exception:
            pass
        return []

    logger.info("🔗 push real-symbol bridge start")
    inspect_global_push_symbols("before-bridge")

    try:
        from core.startup.push_symbol_bridge import install_real_push_symbols
    except Exception:
        logger.exception(
            "❌ push_symbol_bridge import failed. "
            "Create core/startup/push_symbol_bridge.py first."
        )
        try:
            global_data.push_symbol_bridge_installed = False
            global_data.push_symbol_bridge_count = 0
            global_data.push_symbol_bridge_symbols = []
        except Exception:
            pass
        return []

    symbols: list[str] = []

    try:
        ret = install_real_push_symbols(limit=100, strict=False)
        if ret:
            symbols = list(ret)
    except Exception:
        logger.exception("❌ install_real_push_symbols failed")
        symbols = []

    raw, real, filler = count_symbol_quality(symbols)

    try:
        global_data.push_symbol_bridge_installed = bool(real > 0)
        global_data.push_symbol_bridge_count = int(real)
        global_data.push_symbol_bridge_symbols = list(symbols)
    except Exception:
        pass

    logger.info(
        "🔗 push real-symbol bridge complete raw=%d real=%d filler=%d head=%s",
        raw,
        real,
        filler,
        symbols[:10],
    )

    inspect_global_push_symbols("after-bridge")

    if real <= 0:
        logger.error(
            "❌ PUSH登録用の実銘柄が0件です。"
            "symbol_bootstrap は成功していても、active_symbol_manager / daily_watchlist / symbol_flags "
            "から実銘柄を取得できていません。"
        )

    if filler > 0:
        logger.warning(
            "⚠ bridge後のsymbolsにFILLERが残っています filler=%d head=%s",
            filler,
            symbols[:10],
        )

    return symbols


# ============================================================
# push stream bootstrap
# ============================================================

def resolve_push_stream_start_callable() -> Optional[Callable[..., Any]]:
    candidates = (
        ("core.startup.push_stream_bootstrap", "start_push_stream_runner_safe"),
        ("core.startup.push_stream_bootstrap", "start_push_stream_bootstrap"),
        ("core.startup.push_stream_bootstrap", "bootstrap_push_stream"),
        ("core.startup.push_stream_bootstrap", "run_push_stream_bootstrap"),
        ("core.startup.push_stream_bootstrap", "start_push_stream"),
    )

    for module_name, func_name in candidates:
        fn = resolve_attr(module_name, func_name)
        if callable(fn):
            logger.info(
                "[push_startup] resolved push stream bootstrap: %s.%s",
                module_name,
                func_name,
            )
            return fn

    return None


def start_push_stream_runner_safe() -> bool:
    if _should_skip_push_collection_in_main():
        logger.warning(
            "📡 push stream runner bootstrap skipped in main process because "
            "AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1; main_database.py handles PUSH stream."
        )
        try:
            global_data.push_stream_running = False
            global_data.push_stream_skipped_external = True
        except Exception:
            pass
        return True

    fn = resolve_push_stream_start_callable()

    if not callable(fn):
        logger.error(
            "❌ push stream bootstrap function not found. "
            "Check core/startup/push_stream_bootstrap.py"
        )
        try:
            global_data.push_stream_running = False
        except Exception:
            pass
        return False

    try:
        ret = fn()
        ok = bool(ret) if ret is not None else True
    except Exception:
        logger.exception("❌ push stream runner bootstrap failed")
        ok = False

    try:
        global_data.push_stream_running = bool(ok)
        global_data.push_stream_skipped_external = False
    except Exception:
        pass

    logger.info("📡 push stream runner bootstrap result ok=%s", ok)
    return bool(ok)


def start_push_stream_early_safe() -> bool:
    if _should_skip_push_collection_in_main():
        logger.warning(
            "📡 push stream early bootstrap skipped in main process because "
            "AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1; main_database.py handles PUSH stream."
        )
        try:
            global_data.push_stream_early_start_started = False
            global_data.push_stream_early_start_done = False
            global_data.push_stream_early_start_failed = False
            global_data.push_stream_early_start_skipped_external = True
        except Exception:
            pass
        return True

    logger.info("📡 push stream early bootstrap start before startup_summary_restore")

    try:
        global_data.push_stream_early_start_started = True
        global_data.push_stream_early_start_done = False
        global_data.push_stream_early_start_failed = False
        global_data.push_stream_early_start_result = None
    except Exception:
        pass

    ok = False

    try:
        ok = start_push_stream_runner_safe()
    except Exception:
        logger.exception("❌ push stream early bootstrap failed")
        ok = False

    try:
        global_data.push_stream_early_start_done = bool(ok)
        global_data.push_stream_early_start_failed = not bool(ok)
        global_data.push_stream_early_start_result = {
            "ok": bool(ok),
            "phase": "before_startup_summary_restore",
            "at": dt.datetime.now(),
        }
    except Exception:
        pass

    if ok:
        logger.info("✅ push stream early bootstrap complete before startup_summary_restore")
    else:
        logger.warning(
            "⚠ push stream early bootstrap did not complete ok=True. "
            "Fallback bootstrap will retry after startup_summary_restore."
        )

    return bool(ok)


def start_push_stream_fallback_safe() -> bool:
    if _should_skip_push_collection_in_main():
        logger.warning(
            "📡 push stream fallback skipped in main process because "
            "AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1; main_database.py handles PUSH stream."
        )
        return True

    try:
        already_running = bool(getattr(global_data, "push_stream_running", False))
    except Exception:
        already_running = False

    try:
        ws_connected = bool(getattr(global_data, "ws_connected", False))
    except Exception:
        ws_connected = False

    if already_running:
        logger.info(
            "📡 push stream fallback skipped: already running ws_connected=%s",
            ws_connected,
        )
        return True

    logger.info(
        "📡 push stream fallback bootstrap start after startup_summary_restore "
        "already_running=%s ws_connected=%s",
        already_running,
        ws_connected,
    )

    ok = start_push_stream_runner_safe()
    logger.info("📡 push stream fallback bootstrap result ok=%s", ok)
    return bool(ok)


# ============================================================
# public orchestration helper
# ============================================================

def start_push_stack_before_scheduler() -> list[str]:
    """
    PUSH保存 writer / PUSH読込 / symbol bootstrap / bridge / WebSocket を起動する。

    split modeでは、main.py側の PUSH stack 全体を起動しない。
    """
    if _should_skip_push_collection_in_main():
        logger.warning(
            "📡 push collection startup fully skipped in main process: "
            "main_database.py handles DB作成 / PUSH DB read-write / PUSH writer / PUSH stream / registration."
        )
        try:
            global_data.push_collection_skipped_external = True
            global_data.push_writer_running = False
            global_data.push_storage_running = False
            global_data.push_stream_running = False
            global_data.push_symbol_bridge_installed = False
            global_data.push_symbol_bridge_count = 0
            global_data.push_symbol_bridge_symbols = []
        except Exception:
            pass
        return []

    start_push_storage_safe()
    bootstrap_push_safe()
    bootstrap_symbols_safe()

    symbols = install_real_push_symbols_safe()
    if not symbols:
        logger.warning(
            "⚠ PUSH real-symbol bridge returned empty. "
            "push_stream will start, but rotation may wait with no real targets."
        )

    start_push_stream_early_safe()
    return symbols


__all__ = [
    "VERSION",
    "start_push_storage_safe",
    "bootstrap_push_safe",
    "bootstrap_symbols_safe",
    "inspect_global_push_symbols",
    "install_real_push_symbols_safe",
    "resolve_push_stream_start_callable",
    "start_push_stream_runner_safe",
    "start_push_stream_early_safe",
    "start_push_stream_fallback_safe",
    "start_push_stack_before_scheduler",
]
