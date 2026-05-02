# ============================================================
# File   : core/startup/summary_runtime_pkg/runtime.py
# Version: REV3.0-SUMMARY-RUNTIME-BOOTSTRAP-RUNNER
# ------------------------------------------------------------
# 【概要】
#   summary bootstrap の sync/async 実行管理
#
# 【主な機能】
#   - run_bootstrap_summary_sync
#   - start_bootstrap_summary_async
#   - run_bootstrap_summary_fast_boot
#   - bootstrap 前後の DB seed
#   - post-bootstrap hook 呼び出し
# ============================================================

from __future__ import annotations

import logging
import threading

from global_state import global_data

from core.startup.summary_bootstrap import bootstrap_summary

from . import state
from .state import (
    mark_bootstrap_thread_done_ok,
    mark_bootstrap_thread_failed,
    mark_bootstrap_thread_running,
)
from .db_seed import seed_runtime_summary_cache_from_db
from .post_bootstrap import run_post_bootstrap_hook

logger = logging.getLogger(__name__)


def run_bootstrap_summary_sync(*, run_post_hook: bool = True, run_entry_after_bootstrap: bool = True) -> None:
    """
    summary bootstrap を同期実行する。

    Parameters
    ----------
    run_post_hook:
        bootstrap 完了後に scoring / entry hook を実行するか。

    run_entry_after_bootstrap:
        post hook 内で entry pipeline を実行するか。
    """
    mark_bootstrap_thread_running()

    try:
        global_data.summary_bootstrap_running = True
    except Exception:
        pass

    logger.info("📊 summary bootstrap start (sync)")

    try:
        seed_runtime_summary_cache_from_db(
            force=False,
            stage="pre-bootstrap-sync",
            rebuild_missing_scores=False,
        )

        bootstrap_summary()

        mark_bootstrap_thread_done_ok()
        logger.info("✅ summary bootstrap complete (sync)")

        seed_runtime_summary_cache_from_db(
            force=True,
            stage="post-bootstrap-sync",
            rebuild_missing_scores=False,
        )

        if run_post_hook:
            run_post_bootstrap_hook(run_entry=run_entry_after_bootstrap)

    except Exception:
        mark_bootstrap_thread_failed()
        logger.exception("❌ Summary bootstrap failed (sync)")


def start_bootstrap_summary_async(
    force: bool = False,
    *,
    run_post_hook: bool = True,
    run_entry_after_bootstrap: bool = True,
) -> None:
    if state.SUMMARY_BOOTSTRAP_STARTED and not force:
        logger.info("📊 summary bootstrap already started -> async skip")
        return

    if state.SUMMARY_BOOTSTRAP_THREAD is not None:
        try:
            if state.SUMMARY_BOOTSTRAP_THREAD.is_alive() and not force:
                logger.info("📊 summary bootstrap thread alive -> async skip")
                return
        except Exception:
            pass

    # async fast boot では bootstrap 完了前に runtime が動くため、
    # thread 起動前に軽量 DB seed を同期実行する。
    seed_runtime_summary_cache_from_db(
        force=False,
        stage="pre-async-thread",
        rebuild_missing_scores=False,
    )

    def _worker():
        run_bootstrap_summary_sync(
            run_post_hook=run_post_hook,
            run_entry_after_bootstrap=run_entry_after_bootstrap,
        )

    mark_bootstrap_thread_running()

    state.SUMMARY_BOOTSTRAP_THREAD = threading.Thread(
        target=_worker,
        name="summary_bootstrap_async",
        daemon=True,
    )
    state.SUMMARY_BOOTSTRAP_THREAD.start()
    logger.info("📊 summary bootstrap started in background (fast boot path)")


def run_bootstrap_summary_fast_boot(
    *,
    force_sync: bool = False,
    run_post_hook: bool = True,
    run_entry_after_bootstrap: bool = True,
) -> None:
    """
    起動高速化用の入口。
    通常起動は async、強制再構築時だけ sync を許可する。
    """
    if force_sync:
        logger.info("📊 summary bootstrap forced sync path")
        run_bootstrap_summary_sync(
            run_post_hook=run_post_hook,
            run_entry_after_bootstrap=run_entry_after_bootstrap,
        )
        return

    logger.info("📊 summary bootstrap fast-boot path -> async")
    start_bootstrap_summary_async(
        run_post_hook=run_post_hook,
        run_entry_after_bootstrap=run_entry_after_bootstrap,
    )


__all__ = [
    "run_bootstrap_summary_sync",
    "start_bootstrap_summary_async",
    "run_bootstrap_summary_fast_boot",
]