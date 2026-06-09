from __future__ import annotations

import importlib
import logging
import os
import threading
from typing import Callable

logger = logging.getLogger(__name__)
_PATCHED = False
_BG_STARTED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _run_install(label: str, module_name: str, fn_name: str = "install") -> bool:
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, fn_name, None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[FAST STARTUP PATCH] %s installed=%s", label, ok)
        return ok
    except Exception:
        logger.exception("[FAST STARTUP PATCH] %s install failed", label)
        return False


def _install_symbol_flags_bootstrap() -> bool:
    try:
        mod = importlib.import_module("core.startup.symbol_flags_bootstrap")
        fn = getattr(mod, "install_symbol_flags_cache", None)
        ok = bool(fn(force=True)) if callable(fn) else False
        logger.warning("[FAST STARTUP PATCH] symbol_flags_bootstrap installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[FAST STARTUP PATCH] symbol_flags_bootstrap install failed")
        return False


def _patch_summary_schema_bootstrap() -> bool:
    if not _env_bool("FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP", True):
        return False
    try:
        import database.session as ds
        old = getattr(ds, "_bootstrap_summary_schema", None)
        if not callable(old) or getattr(old, "_fast_startup_schema_skip", False):
            return True
        def noop(engine):
            return None
        noop._fast_startup_schema_skip = True
        noop._original_bootstrap = old
        ds._bootstrap_summary_schema = noop
        logger.warning("[FAST STARTUP PATCH] database.session._bootstrap_summary_schema patched to no-op")
        return True
    except Exception:
        logger.exception("[FAST STARTUP PATCH] schema bootstrap patch failed")
        return False


def _patch_entry_max_approved() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        old = getattr(ec, "MAX_APPROVED_PER_RUN", None)
        val = _env_int("ENTRY_MAX_APPROVED_PER_RUN", 10)
        if val <= 0:
            val = 10
        setattr(ec, "MAX_APPROVED_PER_RUN", int(val))
        logger.warning("[FAST STARTUP PATCH] entry_controller.MAX_APPROVED_PER_RUN patched old=%s new=%s", old, getattr(ec, "MAX_APPROVED_PER_RUN", None))
        return True
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry max approved patch failed")
        return False


def _patch_ranking_bootstrap() -> bool:
    try:
        import core.startup.scheduler_bootstrap as sb
        old_lookback = getattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", None)
        new_lookback = _env_int("FAST_STARTUP_RANKING_LOOKBACK_MIN", 60)
        if new_lookback > 0:
            setattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", int(new_lookback))
        old_job = getattr(sb, "_run_ranking_summary_all_job_safe", None)
        if callable(old_job) and not getattr(old_job, "_fast_startup_wrapped", False):
            def no_return(*args, **kwargs):
                old_job(*args, **kwargs)
                return None
            no_return._fast_startup_wrapped = True
            no_return._original = old_job
            sb._run_ranking_summary_all_job_safe = no_return
        logger.warning("[FAST STARTUP PATCH] ranking bootstrap patched lookback %s->%s", old_lookback, getattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", None))
        return True
    except Exception:
        logger.exception("[FAST STARTUP PATCH] ranking bootstrap patch failed")
        return False


def _background_heavy() -> None:
    jobs: list[Callable[[], bool]] = [
        _install_symbol_flags_bootstrap,
        lambda: _run_install("open_position_broker_merge_patch", "core.startup.open_position_broker_merge_patch"),
        lambda: _run_install("exit_executor_broker_position_patch", "core.startup.exit_executor_broker_position_patch"),
        lambda: _run_install("exit_trail_03_runtime_patch", "core.startup.exit_trail_03_runtime_patch"),
        lambda: _run_install("entry_unfilled_cancel_2s_runtime_patch", "core.startup.entry_unfilled_cancel_2s_runtime_patch"),
        lambda: _run_install("entry_liquidity_runtime_patch", "core.startup.entry_liquidity_runtime_patch"),
        lambda: _run_install("entry_affordability_runtime_patch", "core.startup.entry_affordability_runtime_patch"),
        lambda: _run_install("push_summary_direct_ohlc_runtime_patch", "core.startup.push_summary_direct_ohlc_runtime_patch"),
    ]
    logger.warning("[FAST STARTUP PATCH] background heavy installs start count=%s", len(jobs))
    for job in jobs:
        try:
            job()
        except Exception:
            logger.exception("[FAST STARTUP PATCH] background job failed")
    logger.warning("[FAST STARTUP PATCH] background heavy installs done")


def install() -> bool:
    global _PATCHED, _BG_STARTED
    if _PATCHED:
        return True

    _run_install("summary_parallel_intervals_runtime_patch", "core.startup.summary_parallel_intervals_runtime_patch")
    # summary_parallel_intervals_runtime_patch intentionally forces PUSH BG in main-entry-only mode.
    # On this environment, any main.py-side PUSH summary DB/cache path can terminate Windows with
    # 0xC0000006 on NAS SQLite reads. Re-apply the main.py skip immediately after summary_parallel
    # installs so later scheduler ticks cannot start job_summary(PUSH) from main.py.
    _run_install("main_skip_summary_push_bg_patch", "core.startup.main_skip_summary_push_bg_patch")
    _run_install("summary_display_label_guard_patch", "core.startup.summary_display_label_guard_patch")
    _patch_summary_schema_bootstrap()
    _run_install("entry_limit_passive_runtime_patch", "core.startup.entry_limit_passive_runtime_patch")
    _run_install("entry_daily_risk_runtime_patch", "core.startup.entry_daily_risk_runtime_patch")
    _patch_entry_max_approved()
    _patch_ranking_bootstrap()

    if _env_bool("FAST_STARTUP_ASYNC_HEAVY_PATCHES", True):
        if not _BG_STARTED:
            _BG_STARTED = True
            threading.Thread(target=_background_heavy, name="fast-startup-heavy-installs", daemon=True).start()
            logger.warning("[FAST STARTUP PATCH] heavy installs scheduled background")
    else:
        _background_heavy()

    _PATCHED = True
    logger.warning("[FAST STARTUP PATCH] installed v17 async_heavy=%s", _env_bool("FAST_STARTUP_ASYNC_HEAVY_PATCHES", True))
    return True

try:
    install()
except Exception:
    logger.exception("[FAST STARTUP PATCH] auto install failed")

__all__ = ["install"]
