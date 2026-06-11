from __future__ import annotations
import logging, os, threading, time, sys
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
    """Choose an intraday cap.

    V6 keeps build small but allows the controller to run long enough for the
    final safety / board-missing protected path. 2026-06-11 logs showed ranking
    candidates reached PENDING_BUCKET, then timed out at the old 12s controller
    cap while board retry + final safety were still running.
    """
    current_name = name.replace('INTRADAY_', '')
    vals: list[float] = [float(default)]
    for key in (name, fast_name, current_name):
        raw = os.getenv(key)
        if raw is not None and str(raw).strip() != '':
            vals.append(_float_env(key, default))
    # Runtime/build should not widen accidentally. Controller is handled below.
    return str(_clamp(min(vals), lo, hi))


def _patch_controller_timeout_module(controller_cap: str, runtime_cap: str, build_cap: str, max_pending: str) -> bool:
    """Override the older ranking_entry_controller_timeout_patch hard 12s cap.

    The older patch intentionally clamped controller timeout to <=12s.  After
    Yahoo complement was removed from main.py, the remaining bottleneck is the
    controller itself: board REST may return 4002006 at a rotation boundary and
    protected board-missing flow can still proceed, but it needs more than 12s.
    """
    try:
        mod = sys.modules.get('core.startup.ranking_entry_controller_timeout_patch')
        if mod is None:
            return False
        if getattr(mod, '_ranking_controller_relaxed_v6', False):
            return True

        def relaxed_force_runtime_timeouts(tasks) -> None:
            try:
                os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'] = str(build_cap)
                os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'] = str(controller_cap)
                os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = str(runtime_cap)
                os.environ['RANKING_ENTRY_RUNTIME_WARN_SEC'] = str(runtime_cap)
                os.environ['RANKING_ENTRY_RUNTIME_STALE_SEC'] = str(max(float(runtime_cap) + 15.0, 45.0))
                os.environ['RANKING_ENTRY_MAX_PENDING_PER_RUN'] = str(max_pending)
                os.environ['RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN'] = str(max_pending)
                os.environ['RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS'] = '24'
                os.environ['RANKING_ENTRY_FAST_MAX_SYMBOLS'] = '24'
                os.environ['RANKING_ENTRY_FAST_MAX_PER_SIDE'] = '12'
                os.environ['RANKING_ENTRY_FAST_MAX_PER_TYPE'] = '6'
                os.environ['RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS'] = '300'
                tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(build_cap)
                tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(controller_cap)
                try:
                    tasks.RANKING_ENTRY_MAX_PENDING_PER_RUN = int(float(max_pending))
                except Exception:
                    pass
                logger.warning(
                    '[RANKING ENTRY INTRADAY CAP] relaxed controller v6 build=%ss controller=%ss runtime=%ss max_pending=%s',
                    build_cap, controller_cap, runtime_cap, max_pending,
                )
            except Exception:
                logger.debug('[RANKING ENTRY INTRADAY CAP] relaxed force timeout failed', exc_info=True)

        def relaxed_dispatch_ranking_controller(tasks, timeout_sec: float) -> bool:
            try:
                patch_pre = getattr(mod, '_patch_entry_controller_precheck', None)
                if callable(patch_pre):
                    patch_pre()
            except Exception:
                pass
            try:
                t = _clamp(float(timeout_sec or controller_cap), 12.0, float(controller_cap))
            except Exception:
                t = float(controller_cap)
            return bool(tasks._dispatch_entry_controller(pipeline_source='RANKING', interval=1, timeout_sec=t, reason='RANKING ENTRY SCHEDULE'))

        mod._force_runtime_timeouts = relaxed_force_runtime_timeouts
        mod._dispatch_ranking_controller = relaxed_dispatch_ranking_controller
        mod._ranking_controller_relaxed_v6 = True
        logger.warning('[RANKING ENTRY INTRADAY CAP] patched ranking controller hard cap -> controller=%ss runtime=%ss', controller_cap, runtime_cap)
        return True
    except Exception:
        logger.exception('[RANKING ENTRY INTRADAY CAP] patch controller timeout module failed')
        return False


def apply_cap() -> bool:
    runtime = _choose_cap('RANKING_ENTRY_INTRADAY_RUNTIME_BUDGET_SEC', 'RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC', 25.0, 10.0, 25.0)
    build = _choose_cap('RANKING_ENTRY_INTRADAY_BUILD_TIMEOUT_SEC', 'RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC', 18.0, 5.0, 18.0)

    # V6: controller may need to process final safety + board-missing protected
    # path. Keep build bounded, but widen controller to 30s.
    controller = str(_clamp(
        max(
            30.0,
            _float_env('RANKING_ENTRY_INTRADAY_CONTROLLER_TIMEOUT_SEC', 30.0),
            _float_env('RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC', 30.0),
            _float_env('RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC', 30.0),
        ),
        12.0,
        30.0,
    ))
    max_pending = str(int(_clamp(min(_float_env('RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN', 3.0), _float_env('RANKING_ENTRY_MAX_PENDING_PER_RUN', 3.0)), 1.0, 3.0)))

    os.environ['RANKING_ENTRY_INTRADAY_RUNTIME_BUDGET_SEC'] = runtime
    os.environ['RANKING_ENTRY_INTRADAY_BUILD_TIMEOUT_SEC'] = build
    os.environ['RANKING_ENTRY_INTRADAY_CONTROLLER_TIMEOUT_SEC'] = controller
    os.environ['RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC'] = runtime
    os.environ['RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC'] = build
    os.environ['RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC'] = controller
    os.environ['RANKING_ENTRY_RUNTIME_BUDGET_SEC'] = runtime
    os.environ['RANKING_ENTRY_RUNTIME_WARN_SEC'] = runtime
    os.environ['RANKING_ENTRY_RUNTIME_STALE_SEC'] = str(max(float(runtime) + 15.0, 45.0))
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
    patched = _patch_controller_timeout_module(controller, runtime, build, max_pending)
    try:
        import trading.entry_exit.tasks as tasks
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_BUILD_TIMEOUT_SEC'])
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = float(os.environ['RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC'])
    except Exception:
        return False
    return True or patched


def watch():
    loops = max(1, min(int(float(os.getenv('RANKING_ENTRY_INTRADAY_CAP_WATCH_LOOPS', '30') or 30)), 90))
    sleep_sec = max(0.5, min(float(os.getenv('RANKING_ENTRY_INTRADAY_CAP_WATCH_SLEEP_SEC', '1.0') or 1.0), 5.0))
    for i in range(loops):
        ok = apply_cap()
        if i in (0, loops - 1):
            logger.warning(
                '[RANKING ENTRY INTRADAY CAP] enforce v6 i=%s/%s ok=%s runtime=%s build=%s controller=%s max_pending=%s',
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
        '[RANKING ENTRY INTRADAY CAP] installed v6 ok=%s runtime=%s build=%s controller=%s max_pending=%s watcher=True',
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