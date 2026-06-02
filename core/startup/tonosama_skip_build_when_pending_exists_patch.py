from __future__ import annotations
import logging, threading, time
from datetime import datetime, time as dtime

logger = logging.getLogger(__name__)
_DONE = False


def _source(entry) -> str:
    try:
        if isinstance(entry, dict):
            return str(entry.get('source') or entry.get('entry_type') or '').upper()
        return str(getattr(entry, 'source', '') or getattr(entry, 'entry_type', '')).upper()
    except Exception:
        return ''


def _pending_count() -> int:
    total = 0
    try:
        import trading.entry.pending_manager as pm
        it = getattr(pm, 'iter_entries', None)
        if callable(it):
            for _sym, e in list(it()):
                if 'TONOSAMA' in _source(e):
                    total += 1
            return total
    except Exception:
        pass
    try:
        from global_state import global_data
        root = getattr(global_data, 'pending_entries', None)
        if isinstance(root, dict):
            for bucket in root.values():
                entries = bucket if isinstance(bucket, (list, tuple, set)) else [bucket]
                for e in entries:
                    if 'TONOSAMA' in _source(e):
                        total += 1
    except Exception:
        pass
    return total


def _is_entry_time_now() -> bool:
    """JP market entry window only. 11:30-12:30 lunch and after close are blocked."""
    try:
        now = datetime.now().time()
        return (dtime(9, 0) <= now < dtime(11, 30)) or (dtime(12, 30) <= now < dtime(15, 30))
    except Exception:
        return True


def _patch_once() -> bool:
    try:
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, '_run_tonosama_entry_safe', None)
        if not callable(cur):
            return False
        if getattr(cur, '_tonosama_skip_build_when_pending_exists_v2', False):
            return True
        orig = getattr(cur, '_original', cur)

        def patched():
            cnt = _pending_count()
            if cnt > 0:
                if not _is_entry_time_now():
                    logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] pending=%s but market/lunch closed -> keep pending and skip controller dispatch', cnt)
                    return 0
                logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] pending=%s -> dispatch controller only', cnt)
                try:
                    tasks._dispatch_entry_controller(pipeline_source='TONOSAMA', interval=None, timeout_sec=35.0, reason='TONOSAMA ENTRY SCHEDULE pending_exists')
                except Exception:
                    logger.exception('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] controller dispatch failed')
                return 0
            return orig()

        patched._tonosama_skip_build_when_pending_exists_v1 = True
        patched._tonosama_skip_build_when_pending_exists_v2 = True
        patched._original = orig
        tasks._run_tonosama_entry_safe = patched
        logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] patched _run_tonosama_entry_safe v2 market_guard=True')
        return True
    except Exception:
        logger.exception('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] patch failed')
        return False


def _watch():
    for i in range(120):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 30, 60, 119):
            logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] enforce ok=%s market_guard=True', ok)
        time.sleep(0.5)


def install() -> bool:
    global _DONE
    if _DONE:
        return _patch_once()
    ok = _patch_once()
    threading.Thread(target=_watch, name='tonosama-skip-build-when-pending-exists', daemon=True).start()
    _DONE = True
    logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] installed v2 ok=%s watcher=True market_guard=True', ok)
    return True


try:
    install()
except Exception:
    logger.exception('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] auto install failed')
__all__ = ['install']
