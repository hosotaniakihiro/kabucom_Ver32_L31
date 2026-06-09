from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False


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


def apply_patch() -> bool:
    os.environ['SUMMARY_AI_ASYNC_ENTRY'] = '1'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC', '1')
    os.environ['SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'] = os.getenv('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC', '1')
    os.environ['SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS'] = os.getenv('SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS', '2')
    os.environ['SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC'] = os.getenv('SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC', '0.35')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY'] = '0'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX', '3')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_STALE_SEC'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_STALE_SEC', '90')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY'] = '1'
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX', '8')
    os.environ['SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC'] = os.getenv('SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC', '2.0')

    # main.py 側でも summary parent tick を戻す。これが 1 のままだと
    # [main_py_summary_parent_tick_skip] で PUSH summary が空になり、
    # Summary AI / Tonosama が候補0になりやすい。
    os.environ['AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK'] = os.getenv('AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK', '0')
    os.environ['FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK'] = os.getenv('FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK', '1')
    os.environ['ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED'] = os.getenv('ENABLE_PUSH_SUMMARY_FALLBACK_WHEN_UNIFIED_BLOCKED', '1')
    os.environ['SUMMARY_PUSH_BG_ALL_INTERVALS'] = os.getenv('SUMMARY_PUSH_BG_ALL_INTERVALS', '1')

    os.environ.setdefault('ENTRY_ORDER_NO5S_FALLBACK_ENABLED', '1')
    os.environ.setdefault('ENTRY_ORDER_NO5S_FALLBACK_SOURCES', 'RANKING,TONOSAMA')

    _install_optional('SUMMARY_DIRECT_DISPATCH', 'core.startup.summary_ai_async_direct_dispatch_patch')
    _install_optional('FINAL_BOARD_SIGNATURE_LAST', 'core.startup.final_entry_board_guard_signature_last_patch')
    _install_optional('ENTRY_ORDER_NO5S_FALLBACK', 'core.startup.entry_order_no5s_fallback_patch')
    return True


def watch():
    loops = max(1, min(int(float(os.getenv('SUMMARY_AI_NO_DIRECT_SYNC_WATCH_LOOPS', '60') or 60)), 240))
    sleep_sec = max(0.5, min(float(os.getenv('SUMMARY_AI_NO_DIRECT_SYNC_WATCH_SLEEP_SEC', '1.0') or 1.0), 5.0))
    for i in range(loops):
        ok = apply_patch()
        if i in (0, 1, 5, 15, 30, loops - 1):
            logger.warning(
                '[SUMMARY AI NO DIRECT SYNC] enforce v4 i=%s/%s ok=%s direct_sync=%s direct_dispatch=%s parent_skip=%s force_parent=%s no5s=%s',
                i, loops, ok,
                os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'),
                os.environ.get('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'),
                os.environ.get('AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK'),
                os.environ.get('FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK'),
                os.environ.get('ENTRY_ORDER_NO5S_FALLBACK_ENABLED'),
            )
        time.sleep(sleep_sec)


def install() -> bool:
    global _DONE
    if _DONE:
        return apply_patch()
    ok = apply_patch()
    threading.Thread(target=watch, name='summary-ai-direct-sync-compat', daemon=True).start()
    _DONE = True
    logger.warning(
        '[SUMMARY AI NO DIRECT SYNC] installed v4 ok=%s watcher=True direct_sync=%s direct_dispatch=%s parent_skip=%s force_parent=%s no5s=%s',
        ok,
        os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'),
        os.environ.get('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'),
        os.environ.get('AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK'),
        os.environ.get('FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK'),
        os.environ.get('ENTRY_ORDER_NO5S_FALLBACK_ENABLED'),
    )
    return True

try:
    install()
except Exception:
    logger.exception('[SUMMARY AI NO DIRECT SYNC] auto install failed')
__all__ = ['install']