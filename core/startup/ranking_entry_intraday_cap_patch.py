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
    """Do not widen fast-budget settings during the intraday cap install.

    Older v3 forced runtime/build/controller back to 150/180/120 after
    ranking_entry_fast_budget_override_patch had shortened them. That left
    ranking_entry jobs running for 30-40s+ and caused repeated
    previous_still_running skips. v4 keeps the already-short fast cap unless
    an explicit RANKING_ENTRY_INTRADAY_* override is supplied.
    """
    explicit = os.getenv(name)
    if explicit is not None and str(explicit).strip() != '':
        return str(_clamp(_float_env(name, default), lo, hi))

    fast = os.getenv(fast_name)
    if fast is not None and str(fast).strip() != '':
        return str(_clamp(_float_env(fast_name, default), lo, hi))

    current = os.getenv(name.replace('INTRADAY_', ''))
    if current is not None and str(current).strip() != '':
        return str(_clamp(_float_env(name.replace('INTRADAY_', ''), default), lo, hi))

    return str(_clamp(default, lo, hi))


def apply_cap() -> bool:
    # Keep aligned with ranking_entry_fast_budget_override_patch defaults.
    runtime = _choose_cap('RANKING_ENTRY_INTRADAY_RUNTIME_BUDGET_SEC', 'RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC', 25.0, 10.0, 30.0)
    build = _choose_cap('RANKING_ENTRY_INTRADAY_BUILD_TIMEOUT_SEC', 'RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC', 30.0, 15.0, 35.0)
    controller = _choose_cap('RANKING_ENTRY_INTRADAY_CONTROLLER_TIMEOUT_SEC', 'RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC', 30.0, 15.0, 35.0)
    max_pending = str(int(_clamp(_float_env('RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN', 4.0), 1.0, 4.0)))

    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = runtime
    os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'] = build
    os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'] = controller
    os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN'] = max_pending
    os.environ.setdefault('RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC', '20')
    os.environ.setdefault('RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC', '60')
    os.environ.setdefault('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC', '15')
    os.environ.setdefault('SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC', '0.25')
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'])
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'])
    except Exception:
        return False
    return True


def watch():
    loops = max(1, min(int(float(os.getenv('RANKING_ENTRY_INTRADAY_CAP_WATCH_LOOPS', '12') or 12)), 30))
    sleep_sec = max(0.5, min(float(os.getenv('RANKING_ENTRY_INTRADAY_CAP_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = apply_cap()
        if i in (0, loops - 1):
            logger.warning(
                '[RANKING ENTRY INTRADAY CAP] enforce v4 i=%s/%s ok=%s runtime=%s build=%s controller=%s max_pending=%s',
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
        '[RANKING ENTRY INTRADAY CAP] installed v4 ok=%s runtime=%s build=%s controller=%s watcher=True',
        ok,
        os.environ.get('RANKING_ENTRY_RUNTIME_BUDGET_SEC'),
        os.environ.get('RANKING_ENTRY_BUILD_TIMEOUT_SEC'),
        os.environ.get('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'),
    )
    return True

try:
    install()
except Exception:
    logger.exception('[RANKING ENTRY INTRADAY CAP] auto install failed')
__all__ = ['install']
