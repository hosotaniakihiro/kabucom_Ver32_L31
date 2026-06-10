from __future__ import annotations
import logging, os, threading, time
logger = logging.getLogger(__name__)
_DONE = False


def _float_env(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == '' else float(v)
    except Exception:
        return float(default)


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except Exception:
        x = float(lo)
    return max(float(lo), min(float(hi), x))


def _choose_cap(name: str, fast_name: str, default: float, lo: float, hi: float) -> str:
    """Never widen the active 15s ranking-entry timebox.

    V5 keeps fast_budget_override v11 / ranking_stuck_pending_prune v6 aligned.
    Explicit intraday env may only tighten or keep the cap, not widen it.
    """
    current_name = name.replace('INTRADAY_', '')
    vals: list[float] = [float(default)]
    for key in (name, fast_name, current_name):
        raw = os.getenv(key)
        if raw is not None and str(raw).strip() != '':
            vals.append(_float_env(key, default))
    return str(_clamp(min(vals), lo, hi))


def apply_cap() -> bool:
    runtime = _choose_cap('RANKING_ENTRY_INTRADAY_RUNTIME_BUDGET_SEC', 'RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC', 15.0, 5.0, 15.0)
    build = _choose_cap('RANKING_ENTRY_INTRADAY_BUILD_TIMEOUT_SEC', 'RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC', 18.0, 5.0, 18.0)
    controller = _choose_cap('RANKING_ENTRY_INTRADAY_CONTROLLER_TIMEOUT_SEC', 'RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC', 12.0, 5.0, 12.0)
    max_pending = str(int(_clamp(min(_float_env('RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN', 3.0), _float_env('RANKING_ENTRY_MAX_PENDING_PER_RUN', 3.0)), 1.0, 3.0)))

    os.environ['RANKING_ENTRY_INTRADAY_RUNTIME_BUDGET_SEC'] = runtime
    os.environ['RANKING_ENTRY_INTRADAY_BUILD_TIMEOUT_SEC'] = build
    os.environ['RANKING_ENTRY_INTRADAY_CONTROLLER_TIMEOUT_SEC'] = controller
    os.environ['RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC'] = runtime
    os.environ['RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC'] = build
    os.environ['RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC'] = controller
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = runtime
    os.environ['RANKING_ENTRY_RUNTIME_WARN_SEC'] = runtime
    os.environ['RANKING_ENTRY_RUNTIME_STALE_SEC'] = '20.0'
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'] = build
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'] = controller
    os.environ['RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN'] = max_pending
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN'] = max_pending
    os.environ['RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS'] = '24'
    os.environ['RANKING_ENTRY_FAST_MAX_SYMBOLS'] = '24'
    os.environ['RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS'] = '300'
    os.environ['RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC'] = '10'
    os.environ['RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC'] = '30'
    os.environ['SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC'] = str(_clamp(_float_env('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC', 6.0), 1.0, 6.0))
    os.environ.setdefault('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC', '0.25')
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'])
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'])
    except Exception:
        return False
    return True


def watch():
    loops = max(1, min(int(float(os.getenv('RANKING_ENTRY_INTRADAY_CAP_WATCH_LOOPS', '20') or 20)), 60))
    sleep_sec = max(0.5, min(float(os.getenv('RANKING_ENTRY_INTRADAY_CAP_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = apply_cap()
        if i in (0, loops - 1):
            logger.warning(
                '[RANKING ENTRY INTRADAY CAP] enforce v5 i=%s/%s ok=%s runtime=%s build=%s controller=%s max_pending=%s',
                i, loops, ok,
                os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'),
                os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'),
                os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'),
                os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'),
            )
        time.sleep(sleep_sec)


def install() -> bool:
    global _DONE
    if _DONE:
        return apply_cap()
    ok = apply_cap()
    threading.Thread(target=watch, name='ranking-entry-intraday-cap', daemon=True).start()
    _DONE = True
    logger.warning(
        '[RANKING ENTRY INTRADAY CAP] installed v5 ok=%s runtime=%s build=%s controller=%s max_pending=%s watcher=True',
        ok,
        os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'),
        os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'),
        os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'),
        os.environ.get('RANKING_ENTRY_MAX_PENDING_PER_RUN'),
    )
    return True


try:
    install()
except Exception:
    logger.exception('[RANKING ENTRY INTRADAY CAP] auto install failed')
__all__ = ['install']
