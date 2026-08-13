from __future__ import annotations

import importlib
import logging
import os
import threading
from typing import Callable

logger = logging.getLogger(__name__)
_PATCHED = False
_BG_STARTED = False
VERSION = "v23-force-main-direct-push-1m"


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
        mod = importlib.import_module("core.startup.bootstrap.symbol_flags")
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
        import core.startup.scheduler.bootstrap as sb
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
        lambda: _run_install("summary_mtf_push_raw_fallback_patch", "core.startup.summary_mtf_push_raw_fallback_patch"),
        lambda: _run_install("direct_push_1m:bg", "scheduler_jobs.summary.direct_push_1m"),
        # open_position_broker_merge_patch は
        # trading/position/open_position_sync.py へ本文化済み
        # (stale_db_cleanup / sync_throttle と合わせて3層。
        #  スコープは同ファイルの _ops_scope_enabled() が再現)。
        # exit_executor_broker_position_patch は trading/exit/executor.py へ本文化済み
        # (_get_open_position / _close_db_position の broker 実建玉フォールバック。
        #  スコープは同ファイルの _broker_position_fallback_enabled() が再現)。
        lambda: _run_install("exit_trail_03_runtime_patch", "core.startup.exit_trail_03_runtime_patch"),
        # entry_unfilled_cancel_2s_runtime_patch は削除した。
        # 唯一の効果が force_cancel_loop.start_force_cancel_loop の差し替えだったが、
        # main.py:117 が import 節で `from force_cancel_loop import
        # start_force_cancel_loop` と束縛し、main.py:472 でその束縛を使うため、
        # この install (main.py:217 の fast_startup 経由 = import より後) は
        # 実際のスレッド起動関数に届いていなかった。
        # モジュール属性経由で呼ぶ箇所は存在しない (git grep 済み)。
        # entry_liquidity_runtime_patch / summary_ai_liquidity_runtime_patch は削除した。
        # 実体は executor._filter_blocked_ai_ok_items のラップだけで、
        # executor.py:1664 の core filter watcher が install 直後 0.5 秒以内に
        # base へ戻し二度と再 install されないため、完全な死にコードだった。
        # entry_affordability_runtime_patch は trading/entry/summary_ai/candidates.py の
        # build_summary_ai_entry_candidates() へ native化済みのため削除した。
        # push_summary_direct_ohlc_runtime_patch は
        # trading/summary/engine/push_summary_engine.py へ本文化済み
        # (_normalize_push_source_df / _build_direct_ohlc_from_push_source。
        #  スコープは同ファイルの _pdo_scope_enabled() が再現)。
        lambda: _run_install("direct_push_1m:bg-final", "scheduler_jobs.summary.direct_push_1m"),
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

    _run_install("summary_parallel_main_scope:pre", "core.startup.summary_parallel_main_scope")
    _run_install("summary_parallel_intervals_runtime_patch", "core.startup.summary_parallel_intervals_runtime_patch")
    _run_install("summary_parallel_main_scope:post", "core.startup.summary_parallel_main_scope")
    _run_install("light_tick_1m", "scheduler_jobs.summary.light_tick_1m")
    _run_install("tonosama_datetime_tz_guard_patch", "core.startup.tonosama_datetime_tz_guard_patch")

    # main_skip_summary_push_bg_patch は
    # core/startup/summary_parallel_intervals_runtime_patch.py へ統合済み
    # (_split_push_wait_and_bg / _submit_bg_push_interval が
    #  _msk_enabled() で main.py スキップを判定する)。
    # summary_display_label_guard_patch は scheduler_jobs/summary/display.py 本体へ
    # インライン化済みのため削除。
    _patch_summary_schema_bootstrap()
    _run_install("entry_limit_passive_runtime_patch", "core.startup.entry_limit_passive_runtime_patch")
    # entry_daily_risk_runtime_patch は trading/handlers/entry_controller.py の
    # _daily_risk_block_reason / _daily_risk_record_actual_trade へインライン化済み
    # (パッチ側は空スタブだったので削除した)。
    _patch_entry_max_approved()
    _patch_ranking_bootstrap()
    _run_install("summary_mtf_push_raw_fallback_patch", "core.startup.summary_mtf_push_raw_fallback_patch")
    _run_install("direct_push_1m", "scheduler_jobs.summary.direct_push_1m")

    if _env_bool("FAST_STARTUP_ASYNC_HEAVY_PATCHES", True):
        if not _BG_STARTED:
            _BG_STARTED = True
            threading.Thread(target=_background_heavy, name="fast-startup-heavy-installs", daemon=True).start()
            logger.warning("[FAST STARTUP PATCH] heavy installs scheduled background")
    else:
        _background_heavy()

    # Last synchronous re-apply.  Other startup patches may have wrapped job_summary above.
    _run_install("direct_push_1m:final", "scheduler_jobs.summary.direct_push_1m")

    _PATCHED = True
    logger.warning("[FAST STARTUP PATCH] installed %s raw_push_mtf=True async_heavy=%s", VERSION, _env_bool("FAST_STARTUP_ASYNC_HEAVY_PATCHES", True))
    return True

try:
    install()
except Exception:
    logger.exception("[FAST STARTUP PATCH] auto install failed")

__all__ = ["install", "VERSION"]
