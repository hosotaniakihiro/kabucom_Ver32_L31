from __future__ import annotations
import logging, os, sys, threading, time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False
_LAST_PUSH_SUMMARY_REFRESH_TS = 0.0


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


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '':
            return bool(default)
        return str(v).strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'ok', 'enable', 'enabled'}
    except Exception:
        return bool(default)


def _operation_mode() -> str:
    try:
        return str(os.getenv('AUTOSTOCK_MAIN_OPERATION_MODE', 'full') or 'full').strip().lower()
    except Exception:
        return 'full'


def _is_main_py_process() -> bool:
    try:
        return Path(sys.argv[0]).name.lower() == 'main.py'
    except Exception:
        return False


def _main_skip_tonosama_entry() -> bool:
    """
    entry_only 安全モード時だけ main.py の TONOSAMA builder を止める。
    V8: デフォルト full 復帰。AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY を明示した場合のみ優先。
    """
    if not _is_main_py_process():
        return False
    if os.getenv('AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY') is not None:
        return _env_bool('AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY', False)
    return _operation_mode() not in {'full', 'all'} and not _env_bool('AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY', False)


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


def _max_pending() -> int:
    return max(1, min(_env_int('TONOSAMA_MAX_PENDING_PER_LOOP', 2), 4))


def _is_entry_time_now() -> bool:
    try:
        now = datetime.now().time()
        return (dtime(9, 0) <= now < dtime(11, 30)) or (dtime(12, 30) <= now < dtime(15, 30))
    except Exception:
        return True


def _parse_dt(v: Any):
    try:
        if v is None:
            return None
        import pandas as pd
        x = pd.to_datetime(v, errors='coerce')
        if pd.isna(x):
            return None
        return x.to_pydatetime().replace(tzinfo=None)
    except Exception:
        try:
            s = str(v).strip()
            if not s:
                return None
            return datetime.fromisoformat(s.replace('T', ' ')).replace(tzinfo=None)
        except Exception:
            return None


def _latest_push_summary_age_sec() -> tuple[float | None, str | None]:
    """global_data 内のPUSH summary cache最新時刻をざっくり見る。"""
    try:
        from global_state import global_data
    except Exception:
        global_data = None
    if global_data is None:
        return None, None

    candidates = []
    for name in (
        'merged_summary_1min', 'summary_1min_df', 'push_summary_1min_df',
        'merged_summary_3min', 'summary_3min_df',
        'merged_summary_5min', 'summary_5min_df',
    ):
        try:
            df = getattr(global_data, name, None)
            if df is None or getattr(df, 'empty', True) or 'datetime' not in getattr(df, 'columns', []):
                continue
            import pandas as pd
            latest = pd.to_datetime(df['datetime'], errors='coerce').max()
            if pd.notna(latest):
                candidates.append((latest.to_pydatetime().replace(tzinfo=None), name))
        except Exception:
            continue
    if not candidates:
        return None, None
    latest, src = max(candidates, key=lambda x: x[0])
    return max(0.0, (datetime.now() - latest).total_seconds()), f'{src}:{latest}'


def _maybe_refresh_push_summary_for_tonosama(reason: str) -> bool:
    """
    TONOSAMA builder が fresh push summary stale/empty で即skipする前に、
    push DB -> summary cache の差分再構築を一度だけ試す。
    """
    global _LAST_PUSH_SUMMARY_REFRESH_TS
    if not _env_bool('TONOSAMA_REFRESH_STALE_PUSH_SUMMARY_BEFORE_BUILD', True):
        return False
    if not _is_entry_time_now():
        return False

    now = time.time()
    throttle = max(10.0, _env_float('TONOSAMA_PUSH_SUMMARY_REFRESH_THROTTLE_SEC', 45.0))
    if now - float(_LAST_PUSH_SUMMARY_REFRESH_TS or 0.0) < throttle:
        return False

    age, src = _latest_push_summary_age_sec()
    max_age = max(60.0, _env_float('TONOSAMA_PUSH_SUMMARY_FRESH_MAX_AGE_SEC', 300.0))
    if age is not None and age <= max_age:
        return False

    _LAST_PUSH_SUMMARY_REFRESH_TS = now
    try:
        from core.startup.startup_push_incremental_ma75 import build_push_incremental_ma75_on_startup
        logger.warning(
            '[TONOSAMA PUSH SUMMARY REFRESH] start reason=%s age=%s max_age=%.1fs src=%s',
            reason,
            None if age is None else round(age, 1),
            max_age,
            src,
        )
        ret = build_push_incremental_ma75_on_startup(intervals=(1, 3, 5), update_global_cache=True)
        logger.warning(
            '[TONOSAMA PUSH SUMMARY REFRESH] done ok=%s message=%s push_rows=%s new_rows=%s latest=%s cache_rows=%s',
            getattr(ret, 'ok', None),
            getattr(ret, 'message', ''),
            getattr(ret, 'push_rows', None),
            getattr(ret, 'new_rows', None),
            getattr(ret, 'latest', None),
            getattr(ret, 'cache_rows', None),
        )
        return bool(getattr(ret, 'ok', False))
    except Exception:
        logger.exception('[TONOSAMA PUSH SUMMARY REFRESH] failed reason=%s', reason)
        return False


def _mark_and_prune_stuck_tonosama_pending() -> int:
    max_retry = max(1, _env_int('TONOSAMA_STUCK_PENDING_MAX_CONTROLLER_RETRY', 2))
    min_age_sec = max(5.0, _env_float('TONOSAMA_STUCK_PENDING_MIN_AGE_SEC', 20.0))
    max_age_sec = max(min_age_sec, _env_float('TONOSAMA_STUCK_PENDING_MAX_AGE_SEC', 90.0))
    low_score_max_retry = max(1, _env_int('TONOSAMA_STUCK_PENDING_LOW_SCORE_MAX_RETRY', 1))
    low_score = _env_float('TONOSAMA_STUCK_PENDING_LOW_SCORE_THRESHOLD', 3.0)
    now = time.time()

    try:
        import trading.entry.pending_manager as pm
        it = getattr(pm, 'iter_entries', None)
        prune = getattr(pm, 'prune_entries', None)
        if not callable(it) or not callable(prune):
            return 0

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

            if age < min_age_sec:
                return False
            if age >= max_age_sec:
                return True
            if sc > 0 and sc < low_score and retry >= low_score_max_retry:
                return True
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
        if getattr(cur, '_tonosama_skip_build_when_pending_exists_v8', False):
            return True
        orig = getattr(cur, '_original', cur)

        def patched():
            if _main_skip_tonosama_entry():
                logger.warning(
                    '[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] main.py skip tonosama entry job mode=%s. '
                    'Set AUTOSTOCK_MAIN_OPERATION_MODE=full or AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY=0 to restore.',
                    _operation_mode(),
                )
                return 0

            cnt = _pending_count()
            max_pending = _max_pending()
            if cnt > 0:
                pruned = _mark_and_prune_stuck_tonosama_pending()
                if pruned:
                    cnt = _pending_count()
                    if cnt <= 0:
                        logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] stuck pending pruned -> refresh summary and run normal builder')
                        _maybe_refresh_push_summary_for_tonosama('pending_pruned')
                        return orig()

                if not _is_entry_time_now():
                    logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] pending=%s but market/lunch closed -> keep pending and skip controller dispatch', cnt)
                    return 0

                if cnt < max_pending:
                    logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] pending=%s/%s -> refresh summary and run builder to keep rotation', cnt, max_pending)
                    _maybe_refresh_push_summary_for_tonosama('pending_room_build')
                    return orig()

                logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] pending=%s/%s -> dispatch controller only', cnt, max_pending)
                try:
                    timeout = min(_env_float('TONOSAMA_PENDING_CONTROLLER_DISPATCH_TIMEOUT_SEC', 25.0), 15.0)
                    tasks._dispatch_entry_controller(pipeline_source='TONOSAMA', interval=None, timeout_sec=timeout, reason='TONOSAMA ENTRY SCHEDULE pending_exists')
                except Exception:
                    logger.exception('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] controller dispatch failed')
                return 0

            _maybe_refresh_push_summary_for_tonosama('no_pending_before_builder')
            return orig()

        patched._tonosama_skip_build_when_pending_exists_v1 = True
        patched._tonosama_skip_build_when_pending_exists_v2 = True
        patched._tonosama_skip_build_when_pending_exists_v3 = True
        patched._tonosama_skip_build_when_pending_exists_v4 = True
        patched._tonosama_skip_build_when_pending_exists_v5 = True
        patched._tonosama_skip_build_when_pending_exists_v6 = True
        patched._tonosama_skip_build_when_pending_exists_v7 = True
        patched._tonosama_skip_build_when_pending_exists_v8 = True
        patched._original = orig
        tasks._run_tonosama_entry_safe = patched
        logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] patched _run_tonosama_entry_safe v8 market_guard=True stuck_prune=True room_build=True summary_refresh=True main_skip=%s mode=%s', _main_skip_tonosama_entry(), _operation_mode())
        return True
    except Exception:
        logger.exception('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] patch failed')
        return False


def _watch():
    for i in range(120):
        ok = _patch_once()
        if i in (0, 1, 5, 30, 119):
            logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] enforce ok=%s market_guard=True stuck_prune=True v8 summary_refresh=True max_pending=%s main_skip=%s mode=%s', ok, _max_pending(), _main_skip_tonosama_entry(), _operation_mode())
        time.sleep(2.0)


def install() -> bool:
    global _DONE
    if _DONE:
        return True
    os.environ.setdefault('TONOSAMA_REFRESH_STALE_PUSH_SUMMARY_BEFORE_BUILD', '1')
    os.environ.setdefault('TONOSAMA_PUSH_SUMMARY_REFRESH_THROTTLE_SEC', '45')
    os.environ.setdefault('TONOSAMA_PUSH_SUMMARY_FRESH_MAX_AGE_SEC', '300')
    ok = _patch_once()
    threading.Thread(target=_watch, name='tonosama-skip-build-pending-watch', daemon=True).start()
    _DONE = True
    logger.warning('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] installed v8 ok=%s watcher=True market_guard=True stuck_prune=True room_build=True summary_refresh=True fresh_max_age=%s max_pending=%s main_skip=%s mode=%s', ok, os.getenv('TONOSAMA_PUSH_SUMMARY_FRESH_MAX_AGE_SEC'), _max_pending(), _main_skip_tonosama_entry(), _operation_mode())
    return ok


try:
    install()
except Exception:
    logger.exception('[TONOSAMA SKIP BUILD WHEN PENDING EXISTS] auto install failed')

__all__ = ['install']
