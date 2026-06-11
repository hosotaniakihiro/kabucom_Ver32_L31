# ============================================================
# File   : core/startup/ranking_entry_stale_failclosed_final_patch.py
# Version: V8-IGNORE-FUTURE-TIMESTAMPS
# ============================================================
from __future__ import annotations
import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from functools import wraps
from typing import Any
logger = logging.getLogger(__name__)
_INSTALLED = False
_SCHED_STALE_INSTALLED = False
_BUDGET_HARD_STOP_INSTALLED = False
_STARTUP_MONO = time.monotonic()
_STARTUP_WARMUP_LOGGED = False


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == '': return default
    return str(v).strip().lower() in {'1','true','yes','on','enabled'}


def _f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return default if v is None or str(v).strip() == '' else float(v)
    except Exception: return default


def _install_schedule_stale_release() -> bool:
    global _SCHED_STALE_INSTALLED
    try:
        mod = __import__('core.startup.schedule_loop_stale_job_release_patch', fromlist=['install'])
        fn = getattr(mod, 'install', None)
        ok = bool(fn()) if callable(fn) else False
        _SCHED_STALE_INSTALLED = bool(ok)
        if not getattr(_install_schedule_stale_release, '_logged', False):
            logger.warning('[RANKING STALE FINAL] companion schedule stale release installed=%s', ok)
            setattr(_install_schedule_stale_release, '_logged', True)
        return bool(ok)
    except Exception:
        logger.exception('[RANKING STALE FINAL] companion schedule stale release install failed')
        return False


def _install_budget_hard_stop() -> bool:
    global _BUDGET_HARD_STOP_INSTALLED
    try:
        mod = __import__('core.startup.ranking_entry_budget_hard_stop_v6_patch', fromlist=['install'])
        fn = getattr(mod, 'install', None)
        ok = bool(fn()) if callable(fn) else False
        _BUDGET_HARD_STOP_INSTALLED = bool(ok)
        if ok and not getattr(_install_budget_hard_stop, '_logged', False):
            logger.warning('[RANKING STALE FINAL] companion budget hard stop installed=%s', ok)
            setattr(_install_budget_hard_stop, '_logged', True)
        return bool(ok)
    except Exception:
        if _b('RANKING_STALE_FINAL_LOG_OPTIONAL_COMPANION_ERRORS', False):
            logger.exception('[RANKING STALE FINAL] companion budget hard stop install failed')
        return False


def _parse_hhmm(name: str, default: str) -> dt.time:
    s = str(os.getenv(name, default) or default).strip()
    try:
        hh, mm = s.split(':', 1)
        return dt.time(int(hh), int(mm))
    except Exception:
        hh, mm = default.split(':', 1)
        return dt.time(int(hh), int(mm))


def _market_phase(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    t = now.time()
    am_start = _parse_hhmm('RANKING_MARKET_AM_START', '09:00')
    lunch_start = _parse_hhmm('RANKING_MARKET_LUNCH_START', '11:30')
    pm_start = _parse_hhmm('RANKING_MARKET_PM_START', '12:30')
    no_new_after = _parse_hhmm('RANKING_MARKET_NO_NEW_AFTER', os.getenv('ENTRY_NO_NEW_AFTER', '15:20'))
    if t < am_start:
        return 'before_open'
    if am_start <= t < lunch_start:
        return 'am_session'
    if lunch_start <= t < pm_start:
        return 'lunch_break'
    if pm_start <= t < no_new_after:
        return 'pm_session'
    return 'after_no_new'


def _parse_ts(v: Any):
    try:
        if v is None: return None
        s = str(v).strip()
        if not s or s.lower() in {'none','nan','nat'}: return None
        return dt.datetime.fromisoformat(s.replace('T',' ')).replace(tzinfo=None)
    except Exception:
        try:
            import pandas as pd
            x = pd.to_datetime(v, errors='coerce')
            if pd.isna(x): return None
            return x.to_pydatetime().replace(tzinfo=None)
        except Exception: return None


def _fresh_diag() -> tuple[bool, dict[str, Any]]:
    try:
        from ats.ats_ranking.db_path import get_today_ranking_db_path
        db_path = str(get_today_ranking_db_path())
    except Exception: db_path = ''
    now = dt.datetime.now().replace(microsecond=0)
    max_age = _f('RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC', 300.0)
    max_future = _f('RANKING_ENTRY_MAX_FUTURE_SNAPSHOT_SEC', 30.0)
    best = None; source = 'none'; rows = 0; future_best = None; future_source = 'none'; ignored_future = 0
    tables = ('ranking_snapshot_1min','ranking_raw_1min','ranking_summary_1min','ranking_snapshot','ranking_raw')
    cols = ('datetime','updated_at','snapshot_time','received_at','inserted_at','created_at','time')
    try:
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            conn.execute('PRAGMA busy_timeout=800')
            cur = conn.cursor()
            existing = {r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()}
            for table in tables:
                if table not in existing: continue
                try:
                    cnt = int(cur.execute(f'select count(*) from {table}').fetchone()[0] or 0); rows = max(rows, cnt)
                except Exception: cnt = 0
                if cnt <= 0: continue
                try: table_cols = {r[1] for r in cur.execute(f'pragma table_info({table})').fetchall()}
                except Exception: table_cols = set()
                for col in cols:
                    if col not in table_cols: continue
                    try:
                        raw = cur.execute(f'select max({col}) from {table}').fetchone()[0]
                        ts = _parse_ts(raw)
                        if ts is None:
                            continue
                        age_tmp = (now - ts).total_seconds()
                        if age_tmp < -max_future:
                            ignored_future += 1
                            if future_best is None or ts > future_best:
                                future_best = ts; future_source = f'{table}.{col}'
                            continue
                        if best is None or ts > best:
                            best = ts; source = f'{table}.{col}'
                    except Exception: pass
    except Exception:
        logger.exception('[RANKING STALE FINAL] inspect failed db=%s', db_path)
    age = None if best is None else (now - best).total_seconds()
    future_sec = None if future_best is None else abs((now - future_best).total_seconds())
    phase = _market_phase(now)
    ok = bool(best is not None and age is not None and 0 <= float(age) <= max_age and best.date() == now.date())
    if ignored_future:
        logger.warning('[RANKING STALE FINAL] ignored future ranking timestamps count=%s future_latest=%s now=%s future_sec=%s source=%s usable_latest=%s usable_source=%s', ignored_future, future_best, now, None if future_sec is None else round(future_sec, 1), future_source, best, source)
    return ok, {'db': db_path, 'latest': None if best is None else str(best), 'source': source, 'rows': rows, 'age_sec': age, 'future_sec': future_sec, 'future_latest': None if future_best is None else str(future_best), 'future_source': future_source, 'ignored_future_count': ignored_future, 'max_future_sec': max_future, 'max_age_sec': max_age, 'today': str(now.date()), 'phase': phase}


def _latest_is_usable_intraday(diag: dict[str, Any], *, max_age_env: str, max_age_default: float) -> bool:
    try:
        now = dt.datetime.now().replace(microsecond=0)
        latest = _parse_ts(diag.get('latest'))
        if latest is None or latest.date() != now.date():
            return False
        age = diag.get('age_sec')
        if age is None or float(age) < 0:
            return False
        min_latest = dt.datetime.combine(now.date(), _parse_hhmm('RANKING_ENTRY_LUNCH_MIN_LATEST_TIME', '11:00'))
        max_age = _f(max_age_env, max_age_default)
        return bool(latest >= min_latest and float(age) <= max_age)
    except Exception:
        return False


def _is_lunch_reopen_allowed(diag: dict[str, Any]) -> bool:
    if not _b('RANKING_ENTRY_LUNCH_REOPEN_STALE_FAILOPEN', True):
        return False
    try:
        now = dt.datetime.now().replace(microsecond=0)
        if _market_phase(now) != 'pm_session':
            return False
        pm_start = dt.datetime.combine(now.date(), _parse_hhmm('RANKING_MARKET_PM_START', '12:30'))
        grace_min = _f('RANKING_ENTRY_LUNCH_REOPEN_GRACE_MIN', 20.0)
        if now > pm_start + dt.timedelta(minutes=grace_min):
            return False
        return _latest_is_usable_intraday(diag, max_age_env='RANKING_ENTRY_LUNCH_REOPEN_MAX_AGE_SEC', max_age_default=7200.0)
    except Exception:
        return False


def _is_startup_warmup_allowed(diag: dict[str, Any]) -> bool:
    global _STARTUP_WARMUP_LOGGED
    if not _b('RANKING_ENTRY_STARTUP_STALE_GRACE_ENABLED', True):
        return False
    try:
        now = dt.datetime.now().replace(microsecond=0)
        if _market_phase(now) != 'pm_session':
            return False
        uptime_sec = max(0.0, time.monotonic() - _STARTUP_MONO)
        grace_min = _f('RANKING_ENTRY_STARTUP_STALE_GRACE_MIN', 15.0)
        if uptime_sec > grace_min * 60.0:
            return False
        if not _latest_is_usable_intraday(diag, max_age_env='RANKING_ENTRY_STARTUP_STALE_MAX_AGE_SEC', max_age_default=7200.0):
            return False
        if not _STARTUP_WARMUP_LOGGED:
            logger.warning('[RANKING STALE FINAL] startup warmup stale failopen uptime_sec=%.1f diag=%s', uptime_sec, diag)
            _STARTUP_WARMUP_LOGGED = True
        return True
    except Exception:
        return False


def _clear_pending(diag: dict[str, Any]) -> None:
    if not _b('RANKING_ENTRY_CLEAR_PENDING_ON_STALE', True): return
    for mod_name in ('trading.entry.pending_manager','global_state'):
        try:
            mod = __import__(mod_name, fromlist=['x'])
            root = getattr(mod, 'pending_entries', None)
            if root is None and hasattr(mod, 'global_data'): root = getattr(mod.global_data, 'pending_entries', None)
            if isinstance(root, dict):
                before = {str(k): len(v) if hasattr(v, '__len__') else 1 for k, v in root.items()}
                root.clear(); logger.warning('[RANKING STALE FINAL] cleared pending before=%s diag=%s', before, diag)
        except Exception: pass


def _wrap(orig):
    @wraps(orig)
    def wrapped(*args, **kwargs):
        _install_schedule_stale_release()
        _install_budget_hard_stop()
        if _b('RANKING_ENTRY_STALE_FAILOPEN_ENABLED', False): return orig(*args, **kwargs)
        phase = _market_phase()
        if phase in {'before_open', 'lunch_break', 'after_no_new'}:
            logger.info('[RANKING STALE FINAL] market phase skip fast phase=%s', phase)
            return 0
        ok, diag = _fresh_diag()
        if not ok:
            if _is_lunch_reopen_allowed(diag):
                logger.warning('[RANKING STALE FINAL] lunch reopen stale failopen diag=%s', diag)
                return orig(*args, **kwargs)
            if _is_startup_warmup_allowed(diag):
                return orig(*args, **kwargs)
            logger.warning('[RANKING STALE FINAL] skip ranking entry before pending diag=%s', diag)
            _clear_pending(diag); return 0
        logger.info('[RANKING STALE FINAL] ranking source fresh diag=%s', diag)
        return orig(*args, **kwargs)
    wrapped._ranking_stale_final_v1 = True
    wrapped._ranking_stale_final_v2 = True
    wrapped._ranking_stale_final_v3 = True
    wrapped._ranking_stale_final_v4 = True
    wrapped._ranking_stale_final_v5 = True
    wrapped._ranking_stale_final_v6 = True
    wrapped._ranking_stale_final_v7 = True
    wrapped._ranking_stale_final_v8 = True
    wrapped._original = orig
    return wrapped


def _patch_once() -> bool:
    try:
        _install_schedule_stale_release()
        _install_budget_hard_stop()
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, '_run_ranking_entry_safe', None)
        if not callable(cur): return False
        if getattr(cur, '_ranking_stale_final_v8', False): return True
        base = getattr(cur, '_original', cur) if (getattr(cur, '_ranking_stale_final_v7', False) or getattr(cur, '_ranking_stale_final_v6', False) or getattr(cur, '_ranking_stale_final_v5', False) or getattr(cur, '_ranking_stale_final_v4', False) or getattr(cur, '_ranking_stale_final_v3', False) or getattr(cur, '_ranking_stale_final_v2', False) or getattr(cur, '_ranking_stale_final_v1', False)) else cur
        setattr(tasks, '_run_ranking_entry_safe', _wrap(base)); return True
    except Exception:
        logger.exception('[RANKING STALE FINAL] patch failed')
        return False


def _watcher():
    loops = int(_f('RANKING_STALE_FINAL_WATCH_LOOPS', 8))
    sleep = _f('RANKING_STALE_FINAL_WATCH_SLEEP_SEC', 2.0)
    for i in range(max(1, loops)):
        ok = _patch_once()
        if i == 0 or not ok:
            logger.warning('[RANKING STALE FINAL] enforce v8 i=%s/%s ok=%s schedule_stale=%s budget_hard_stop=%s', i, loops, ok, _SCHED_STALE_INSTALLED, _BUDGET_HARD_STOP_INSTALLED)
        time.sleep(max(0.2, sleep))


def install() -> bool:
    global _INSTALLED
    ok = _patch_once()
    if not _INSTALLED:
        _INSTALLED = True
        if _b('RANKING_STALE_FINAL_WATCHER', True):
            threading.Thread(target=_watcher, name='ranking-stale-final-watch', daemon=True).start()
        logger.warning('[RANKING STALE FINAL] installed v8 ok=%s failopen=%s schedule_stale=%s lunch_reopen=%s startup_grace=%s budget_hard_stop=%s max_future=%s', ok, _b('RANKING_ENTRY_STALE_FAILOPEN_ENABLED', False), _SCHED_STALE_INSTALLED, _b('RANKING_ENTRY_LUNCH_REOPEN_STALE_FAILOPEN', True), _b('RANKING_ENTRY_STARTUP_STALE_GRACE_ENABLED', True), _BUDGET_HARD_STOP_INSTALLED, _f('RANKING_ENTRY_MAX_FUTURE_SNAPSHOT_SEC', 30.0))
    return bool(ok)


__all__ = ['install']