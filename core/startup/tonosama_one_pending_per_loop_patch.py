from __future__ import annotations
import logging, os, threading, time

logger = logging.getLogger(__name__)
_DONE = False
_WATCHER_STARTED = False
_LAST_STATE = None


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '':
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def apply_patch(log_change: bool = True) -> bool:
    global _LAST_STATE
    # 1件固定だと、板待ち/キャンセル待ち/pending残りでTONOSAMA候補生成が止まりやすい。
    # デフォルトは2件まで許可し、環境変数で1-4件に調整可能にする。
    max_pending = max(1, min(_env_int('TONOSAMA_MAX_PENDING_PER_LOOP', 2), 4))
    os.environ['TONOSAMA_MAX_PENDING_PER_LOOP'] = str(max_pending)
    os.environ['TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES'] = os.getenv('TONOSAMA_ONE_PENDING_MAX_EVAL', '8')
    try:
        import trading.entry.tonosama.runner as runner
        old = getattr(runner, 'MAX_PENDING_PER_LOOP', None)
        runner.MAX_PENDING_PER_LOOP = max_pending
        state = (str(getattr(runner, 'MAX_PENDING_PER_LOOP', None)), str(os.environ.get('TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES')))
        if log_change and state != _LAST_STATE:
            logger.warning('[TONOSAMA PENDING LIMIT] apply old=%s new=%s max_eval=%s', old, getattr(runner, 'MAX_PENDING_PER_LOOP', None), os.environ.get('TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES'))
        _LAST_STATE = state
    except Exception:
        return False
    return True


def watch():
    loops = max(1, min(int(float(os.getenv('TONOSAMA_ONE_PENDING_WATCH_LOOPS', '12') or 12)), 30))
    sleep_sec = max(0.5, min(float(os.getenv('TONOSAMA_ONE_PENDING_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = apply_patch(log_change=False)
        if i in (0, loops - 1):
            logger.warning('[TONOSAMA PENDING LIMIT] enforce v3 i=%s/%s ok=%s max_pending=%s', i, loops, ok, os.environ.get('TONOSAMA_MAX_PENDING_PER_LOOP'))
        time.sleep(sleep_sec)


def install() -> bool:
    global _DONE, _WATCHER_STARTED
    if _DONE and _WATCHER_STARTED:
        return True
    os.environ.setdefault('TONOSAMA_MAX_PENDING_PER_LOOP', '2')
    os.environ.setdefault('TONOSAMA_ONE_PENDING_MAX_EVAL', '8')
    ok = apply_patch(log_change=True)
    if not _WATCHER_STARTED:
        threading.Thread(target=watch, name='tonosama-pending-limit', daemon=True).start()
        _WATCHER_STARTED = True
    _DONE = True
    logger.warning('[TONOSAMA PENDING LIMIT] installed v3 ok=%s watcher=%s max_pending=%s', ok, _WATCHER_STARTED, os.environ.get('TONOSAMA_MAX_PENDING_PER_LOOP'))
    return True


try:
    install()
except Exception:
    logger.exception('[TONOSAMA PENDING LIMIT] auto install failed')

__all__ = ['install']
