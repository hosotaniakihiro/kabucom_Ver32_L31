from __future__ import annotations
import logging, os, threading, time
from datetime import datetime, time as dtime
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '':
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '':
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _source(entry: Any) -> str:
    try:
        if isinstance(entry, dict):
            return str(entry.get('source') or entry.get('entry_type') or '').upper()
        return str(getattr(entry, 'source', '') or getattr(entry, 'entry_type', '')).upper()
    except Exception:
        return ''


def _score(entry: Any) -> float:
    try:
        if isinstance(entry, dict):
            return float(entry.get('score') or entry.get('_tonosama_score') or entry.get('pending_score') or 0.0)
    except Exception:
        pass
    return 0.0


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
    try:
        now = datetime.now().time()
        return (dtime(9, 0) <= now < dtime(11, 30)) or (dtime(12, 30) <= now < dtime(15, 30))
    except Exception:
        return True


def _mark_and_prune_stuck_tonosama_pending() -> int:
    """
    ATR/RANGE等で落ち続けるTONOSAMA pendingは、pending_existsループにより
    新規TONOSAMA候補生成を止める。ここで一定回数以上再評価済みの候補を掃除する。

    v4:
      - retry回数だけでは削除しない。
      - pending作成直後にcontrollerが複数回走ると即pruneされる問題を防ぐ。
      - 最低滞留時間を過ぎた上で retry上限、または最大滞留時間で削除する。
    """
    max_retry = max(1, _env_int('TONOSAMA_STUCK_PENDING_MAX_CONTROLLER_RETRY', 3))
    min_age_sec = max(5.0, _env_float('TONOSAMA_STUCK_PENDING_MIN_AGE_SEC', 30.0))
    max_age_sec = max(min_age_sec, _env_float('TONOSAMA_STUCK_PENDING_MAX_AGE_SEC', 120.0))
    low_score_max_retry = max(1, _env_int('TONOSAMA_STUCK_PENDING_LOW_SCORE_MAX_RETRY', 2))
    low_score = _env_float('TONOSAMA_STUCK_PENDING_LOW_SCORE_THRESHOLD', 3.0)
    now = time.time()

    try:
        import trading.entry.pending_manager as pm
        it = getattr(pm, 'iter_entries', None)
        prune = getattr(pm, 'prune_entries', None)
        if not callable(it) or not callable(prune):
            return 0

        # まず各entryへcontroller-only試行回数と初回時刻を付与する。
        for _sym, entry in list(it()):
            if not isinstance(entry, dict) or 'TONOSAMA' not in _source(entry):
                continue
            first = entry.get('_tonosama_pending_first_seen_ts')
            if not first:
                entry['_tonosama_pending_first_seen_ts'] = now
                first = now
            entry['_tonosama_controller_retry_count'] = int(float(entry.get('_tonosama_controller_retry_count') or 0)) + 1
            entry['_tonosama_last_controller_retry_ts'] = now
            logger.info(
                '[TONOSAMA STUCK PENDING] mark symbol=%s retry=%s age=%.1fs score=%.4f min_age=%.1fs max_age=%.1fs',
                _sym,
                entry.get('_tonosama_controller_retry_count'),
                now - float(first),
                _score(entry),
                min_age_sec,
                max_age_sec,
            )

        def pred(sym: str, entry: dict) -> bool:
            if not isinstance(entry, dict) or 'TONOSAMA' not in _source(entry):
                return False
            retry = int(float(entry.get('_tonosama_controller_retry_count') or 0))
            first = float(entry.get('_tonosama_pending_first_seen_ts') or now)
            age = now - first
            sc = _score(entry)

            # 作成直後は絶対に消さない。発注executorへ渡る猶予を必ず残す。
            if age < min_age_sec:
                return False

            # 最大滞留時間を超えたpendingは掃除する。
            if age >= max_age_sec:
                return True

            # 低スコアTONOSAMAも最低滞留時間後に限って掃除する。
            if sc > 0 and sc < low_score and retry >= low_score_max_retry:
                return True

            # retry上限だけではなく、最低滞留時間経過後に限って掃除する。
            if retry >= max_retry:
                return True

            return False

        removed = int(prune(pred, reason='TONOSAMA_STUCK_PENDING_RETRY_OR_AGE'))
        if removed:
            logger.warning(
                '[TONOSAMA STUCK PENDING] pruned removed=%s max_retry=%s min_age=%.1fs max_age=%.1fs low_score<%.2f low_score_retry=%s',
                removed,
                max_retry,
                min_age_sec,
                max_age_sec,
                low_score,
                low_score_max_retry,
            )
        return removed
    except Exception:
        logger.exception('[TONOSAMA STUCK PENDING] prune failed')
        return 0


def _patch_once() -> bool:
    try:
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, '_run_tonosama_entry_safe', None)
        if not callable(cur):
            return False
        if getattr(cur, '_tonosama_skip_build_when_pending_exists_v4', False):
            return True
        orig = getattr(cur, '_original', cur)

        def patched():
            cnt = _pending_count()
            if cnt > 0:
                pruned = _mark_and_prune_stuck_tonosama_pending()
                if pruned:
                    cnt = _pending_count()
                    if cnt <= 0:
                        logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] stuck pending pruned -> run normal builder')
                        return orig()
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
        patched._tonosama_skip_build_when_pending_exists_v3 = True
        patched._tonosama_skip_build_when_pending_exists_v4 = True
        patched._original = orig
        tasks._run_tonosama_entry_safe = patched
        logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] patched _run_tonosama_entry_safe v4 market_guard=True stuck_prune=True min_age_guard=True')
        return True
    except Exception:
        logger.exception('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] patch failed')
        return False


def _watch():
    for i in range(120):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 30, 60, 119):
            logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] enforce ok=%s market_guard=True stuck_prune=True v4', ok)
        time.sleep(0.5)


def install() -> bool:
    global _DONE
    if _DONE:
        return _patch_once()
    os.environ.setdefault('TONOSAMA_STUCK_PENDING_MAX_CONTROLLER_RETRY', '3')
    os.environ.setdefault('TONOSAMA_STUCK_PENDING_LOW_SCORE_MAX_RETRY', '2')
    os.environ.setdefault('TONOSAMA_STUCK_PENDING_LOW_SCORE_THRESHOLD', '3.0')
    os.environ.setdefault('TONOSAMA_STUCK_PENDING_MIN_AGE_SEC', '30')
    os.environ.setdefault('TONOSAMA_STUCK_PENDING_MAX_AGE_SEC', '120')
    ok = _patch_once()
    threading.Thread(target=_watch, name='tonosama-skip-build-when-pending-exists', daemon=True).start()
    _DONE = True
    logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] installed v4 ok=%s watcher=True market_guard=True stuck_prune=True min_age_guard=True', ok)
    return True


try:
    install()
except Exception:
    logger.exception('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] auto install failed')
__all__ = ['install']