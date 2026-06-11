# ============================================================
# File   : core/startup/ranking_entry_stale_snapshot_skip_patch.py
# Version: V7-IGNORE-FUTURE-TIMESTAMPS
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
_ORIG_RUN = None

def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '': return bool(default)
        return str(v).strip().lower() in {'1','true','yes','y','on','ok','enable','enabled'}
    except Exception: return bool(default)

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == '' else float(v)
    except Exception: return float(default)

def _parse_dt(v: Any) -> dt.datetime | None:
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

def _latest_snapshot_time(db_path: str, *, now: dt.datetime, max_future_sec: float) -> tuple[dt.datetime | None, str, int, dt.datetime | None, str, int]:
    if not db_path: return None, 'no_db_path', 0, None, 'no_db_path', 0
    cols = ['updated_at','datetime','snapshot_time','received_at','inserted_at','created_at','time']
    tables = ['ranking_snapshot_1min','ranking_raw_1min','ranking_summary_1min','ranking_snapshot','ranking_raw']
    try:
        with sqlite3.connect(str(db_path), timeout=2.0) as conn:
            cur = conn.cursor(); existing = {r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()}
            best_dt = None; best_src = 'no_time'; best_rows = 0; table_counts = {}
            future_dt = None; future_src = 'no_future_time'; future_count = 0
            for table in tables:
                if table not in existing: continue
                try: cnt = int(cur.execute(f'select count(*) from {table}').fetchone()[0] or 0)
                except Exception: cnt = 0
                table_counts[table] = cnt; best_rows = max(best_rows, cnt)
                if cnt <= 0: continue
                try: table_cols = {r[1] for r in cur.execute(f'pragma table_info({table})').fetchall()}
                except Exception: table_cols = set()
                for col in cols:
                    if col not in table_cols: continue
                    try:
                        parsed = _parse_dt(cur.execute(f'select max({col}) from {table}').fetchone()[0])
                        if parsed is None: continue
                        future_sec = (parsed - now).total_seconds()
                        if future_sec > max_future_sec:
                            future_count += 1
                            if future_dt is None or parsed > future_dt:
                                future_dt = parsed; future_src = f'{table}.{col}'
                            continue
                        if best_dt is None or parsed > best_dt:
                            best_dt = parsed; best_src = f'{table}.{col}'
                    except Exception: continue
            if best_rows <= 0: logger.warning('[RANKING STALE SNAPSHOT SKIP] ranking db empty diag path=%s tables=%s', db_path, table_counts)
            return best_dt, best_src, best_rows, future_dt, future_src, future_count
    except Exception:
        logger.exception('[RANKING STALE SNAPSHOT SKIP] db inspect failed path=%s', db_path)
        return None, 'inspect_error', 0, None, 'inspect_error', 0

def _ranking_snapshot_fresh() -> tuple[bool, dict[str, Any]]:
    try:
        from ats.ats_ranking.db_path import get_usable_ranking_db_path
        db_path = get_usable_ranking_db_path(force_refresh=True, allow_fallback=False, prefer_today_even_if_empty=True)
    except Exception:
        logger.exception('[RANKING STALE SNAPSHOT SKIP] resolve db failed'); db_path = None
    max_age = _env_float('RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC', _env_float('RANKING_PRECHECK_MAX_AGE_SEC', 300.0))
    max_future = _env_float('RANKING_ENTRY_MAX_FUTURE_SNAPSHOT_SEC', 30.0)
    now = dt.datetime.now()
    latest, src, rows, future_latest, future_src, ignored_future_count = _latest_snapshot_time(str(db_path or ''), now=now, max_future_sec=max_future)
    age = None if latest is None else (now - latest).total_seconds()
    require_today = _env_bool('RANKING_ENTRY_REQUIRE_TODAY', True)
    same_day = latest is not None and latest.date() == now.date()
    ok = latest is not None and age is not None and 0 <= age <= max_age and (same_day or not require_today)
    future_sec = None if future_latest is None else (future_latest - now).total_seconds()
    if future_latest is not None:
        logger.warning(
            '[RANKING STALE SNAPSHOT SKIP] ignored future ranking timestamps count=%s future_latest=%s now=%s future_sec=%.1f source=%s usable_latest=%s usable_source=%s',
            ignored_future_count, future_latest, now, float(future_sec or 0.0), future_src, latest, src,
        )
    return bool(ok), {
        'ok': bool(ok), 'db': str(db_path or ''), 'latest': latest.isoformat(sep=' ') if latest else None,
        'source': src, 'rows': rows, 'age_sec': None if age is None else round(float(age), 3),
        'future_sec': None if future_sec is None else round(float(future_sec), 3),
        'future_latest': future_latest.isoformat(sep=' ') if future_latest else None,
        'future_source': future_src if future_latest else None,
        'ignored_future_count': ignored_future_count,
        'max_future_sec': max_future, 'max_age_sec': max_age, 'require_today': require_today, 'same_day': bool(same_day)
    }

def _legacy_fail_closed_allowed() -> bool:
    return bool(_env_bool('RANKING_ENTRY_FORCE_FAIL_CLOSED_ON_STALE', False) or (_env_bool('ALLOW_LEGACY_RANKING_STALE_FAIL_CLOSED', False) and _env_bool('RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE', False)))

def _clear_ranking_pending(reason: str, diag: dict[str, Any]) -> None:
    if not _env_bool('RANKING_ENTRY_CLEAR_PENDING_ON_STALE', False): return
    for mod_name in ('trading.entry.pending_manager','global_state'):
        try:
            mod = __import__(mod_name, fromlist=['x'])
            root = getattr(mod, 'pending_entries', None)
            if root is None and hasattr(mod, 'global_data'): root = getattr(mod.global_data, 'pending_entries', None)
            if isinstance(root, dict):
                before = {str(k): len(v) if hasattr(v, '__len__') else 1 for k, v in root.items()}
                root.clear(); logger.warning('[RANKING STALE SNAPSHOT SKIP] cleared pending reason=%s before=%s diag=%s', reason, before, diag)
        except Exception: pass

def _make_wrapper(orig):
    @wraps(orig)
    def wrapped_run_ranking_entry_safe(*args: Any, **kwargs: Any):
        try:
            ok, diag = _ranking_snapshot_fresh()
            if not ok:
                if _legacy_fail_closed_allowed():
                    logger.warning('[RANKING STALE SNAPSHOT SKIP] explicit fail-closed skip ranking entry diag=%s', diag)
                    _clear_ranking_pending('ranking_snapshot_stale', diag); return 0
                logger.warning('[RANKING STALE SNAPSHOT SKIP] stale/empty but FORCE FAIL-OPEN continue ranking entry diag=%s', diag)
            else:
                logger.info('[RANKING STALE SNAPSHOT SKIP] ranking snapshot fresh diag=%s', diag)
        except Exception:
            if _legacy_fail_closed_allowed():
                logger.exception('[RANKING STALE SNAPSHOT SKIP] precheck failed -> explicit fail-closed ranking entry'); return 0
            logger.exception('[RANKING STALE SNAPSHOT SKIP] precheck failed -> FORCE FAIL-OPEN ranking entry')
        return orig(*args, **kwargs)
    wrapped_run_ranking_entry_safe._ranking_stale_snapshot_skip_v4 = True
    wrapped_run_ranking_entry_safe._ranking_stale_snapshot_skip_v5 = True
    wrapped_run_ranking_entry_safe._ranking_stale_snapshot_skip_v6 = True
    wrapped_run_ranking_entry_safe._ranking_stale_snapshot_skip_v7 = True
    wrapped_run_ranking_entry_safe._original = orig
    return wrapped_run_ranking_entry_safe

def _patch_once() -> bool:
    global _ORIG_RUN
    try:
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, '_run_ranking_entry_safe', None)
        if not callable(cur): return False
        if getattr(cur, '_ranking_stale_snapshot_skip_v7', False): return True
        base = getattr(cur, '_original', cur) if (getattr(cur, '_ranking_stale_snapshot_skip_v6', False) or getattr(cur, '_ranking_stale_snapshot_skip_v5', False) or getattr(cur, '_ranking_stale_snapshot_skip_v4', False)) else cur
        _ORIG_RUN = base; tasks._run_ranking_entry_safe = _make_wrapper(base)
        logger.warning('[RANKING STALE SNAPSHOT SKIP] patched outermost v7 target=%s', getattr(base, '__name__', type(base)))
        return True
    except Exception:
        logger.exception('[RANKING STALE SNAPSHOT SKIP] patch_once failed'); return False

def _watch() -> None:
    loops = max(1, min(int(float(os.getenv('RANKING_STALE_SNAPSHOT_WATCH_LOOPS', '8') or 8)), 30))
    sleep_sec = max(0.5, min(float(os.getenv('RANKING_STALE_SNAPSHOT_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = _patch_once()
        if i in (0, loops - 1): logger.warning('[RANKING STALE SNAPSHOT SKIP] enforce v7 i=%s/%s ok=%s', i, loops, ok)
        time.sleep(sleep_sec)

def install() -> bool:
    global _INSTALLED
    try:
        os.environ['RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE'] = '0'
        os.environ.setdefault('ALLOW_LEGACY_RANKING_STALE_FAIL_CLOSED', '0')
        os.environ.setdefault('RANKING_ENTRY_FORCE_FAIL_CLOSED_ON_STALE', '0')
        os.environ.setdefault('RANKING_ENTRY_REQUIRE_TODAY', '1')
        os.environ.setdefault('RANKING_ENTRY_CLEAR_PENDING_ON_STALE', '0')
        os.environ.setdefault('RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC', '300')
        os.environ.setdefault('RANKING_ENTRY_MAX_FUTURE_SNAPSHOT_SEC', '30')
        if _INSTALLED: return True
        ok = _patch_once(); _INSTALLED = True
        threading.Thread(target=_watch, name='ranking-stale-snapshot-skip-enforcer', daemon=True).start()
        logger.warning('[RANKING STALE SNAPSHOT SKIP] installed v7 ok=%s max_age=%s max_future=%s', ok, os.getenv('RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC'), os.getenv('RANKING_ENTRY_MAX_FUTURE_SNAPSHOT_SEC'))
        return True
    except Exception:
        logger.exception('[RANKING STALE SNAPSHOT SKIP] install failed'); return False
try: install()
except Exception: logger.exception('[RANKING STALE SNAPSHOT SKIP] auto install failed')
__all__ = ['install']