# ============================================================
# File   : core/startup/fast_startup_runtime_patch.py
# Version: PRODUCTION-FAST-STARTUP-PATCH-V11-ENTRY-CANCEL-2S-NEXT
# ------------------------------------------------------------
# 目的:
#   main.py 起動直後の重い処理を軽くする。
#   さらに、OPEN建玉同期に broker 実建玉マージpatchを入れる。
#   さらに、EXIT_EXECUTOR が内部建玉を見つけられない場合でも、
#   broker信用建玉から復元して返済注文できるpatchを起動時に入れる。
#   さらに、AI確認前に予算上限で買えない高価格銘柄を除外するpatchを入れる。
#   さらに、symbol_flags.db を起動時に global_data へキャッシュする。
#   さらに、PUSH rows があるのに summary が0件になる場合の direct OHLC patch を入れる。
#   さらに、定時サマリーAI許可銘柄の最大発注数を10にする。
#   さらに、エントリー価格0.3%損切り・高値/安値0.3%トレーリングEXITを入れる。
#   さらに、SUMMARY_AIのエントリー指値を BUY=ask-1tick / SELL=bid+1tick にする。
#   さらに、未約定新規指値を2秒で取消し、次候補のENTRYを起動する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _patch_summary_schema_bootstrap() -> None:
    skip_schema = _env_bool("FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP", True)
    if not skip_schema:
        logger.warning(
            "[FAST STARTUP PATCH] summary schema bootstrap skip disabled env=FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP"
        )
        return

    try:
        import database.session as ds
    except Exception:
        logger.exception("[FAST STARTUP PATCH] database.session import failed for schema skip")
        return

    old_bootstrap = getattr(ds, "_bootstrap_summary_schema", None)
    if not callable(old_bootstrap):
        logger.warning("[FAST STARTUP PATCH] _bootstrap_summary_schema not callable")
        return

    if getattr(old_bootstrap, "_fast_startup_schema_skip", False):
        return

    def _skip_summary_schema_bootstrap(engine):
        logger.warning(
            "[FAST STARTUP PATCH] summary schema bootstrap skipped in main.py "
            "reason=main_database_handles_schema env=FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP"
        )
        return None

    _skip_summary_schema_bootstrap._fast_startup_schema_skip = True  # type: ignore[attr-defined]
    _skip_summary_schema_bootstrap._original_bootstrap = old_bootstrap  # type: ignore[attr-defined]
    ds._bootstrap_summary_schema = _skip_summary_schema_bootstrap

    logger.warning("[FAST STARTUP PATCH] database.session._bootstrap_summary_schema patched to no-op")


def _install_symbol_flags_bootstrap() -> None:
    try:
        from core.startup.symbol_flags_bootstrap import install_symbol_flags_cache

        ok = install_symbol_flags_cache(force=True)
        logger.warning("[FAST STARTUP PATCH] symbol_flags_bootstrap installed=%s", ok)
    except Exception:
        logger.exception("[FAST STARTUP PATCH] symbol_flags_bootstrap install failed")


def _install_open_position_broker_merge_patch() -> None:
    try:
        from core.startup.open_position_broker_merge_patch import install as install_open_position_patch

        ok = install_open_position_patch()
        logger.warning("[FAST STARTUP PATCH] open_position_broker_merge_patch installed=%s", ok)
    except Exception:
        logger.exception("[FAST STARTUP PATCH] open_position_broker_merge_patch install failed")


def _install_exit_executor_broker_position_patch() -> None:
    try:
        from core.startup.exit_executor_broker_position_patch import install as install_exit_executor_patch

        ok = install_exit_executor_patch()
        logger.warning("[FAST STARTUP PATCH] exit_executor_broker_position_patch installed=%s", ok)
    except Exception:
        logger.exception("[FAST STARTUP PATCH] exit_executor_broker_position_patch install failed")


def _install_exit_trail_03_patch() -> None:
    try:
        from core.startup.exit_trail_03_runtime_patch import install as install_exit_trail_03_patch

        ok = install_exit_trail_03_patch()
        logger.warning("[FAST STARTUP PATCH] exit_trail_03_runtime_patch installed=%s", ok)
    except Exception:
        logger.exception("[FAST STARTUP PATCH] exit_trail_03_runtime_patch install failed")


def _install_entry_passive_limit_patch() -> None:
    try:
        from core.startup.entry_limit_passive_runtime_patch import install as install_entry_passive_limit_patch

        ok = install_entry_passive_limit_patch()
        logger.warning("[FAST STARTUP PATCH] entry_limit_passive_runtime_patch installed=%s", ok)
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry_limit_passive_runtime_patch install failed")


def _install_entry_cancel_2s_next_patch() -> None:
    try:
        from core.startup.entry_unfilled_cancel_2s_runtime_patch import install as install_entry_cancel_2s_patch

        ok = install_entry_cancel_2s_patch()
        logger.warning("[FAST STARTUP PATCH] entry_unfilled_cancel_2s_runtime_patch installed=%s", ok)
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry_unfilled_cancel_2s_runtime_patch install failed")


def _install_entry_affordability_patch() -> None:
    try:
        from core.startup.entry_affordability_runtime_patch import install as install_affordability_patch
        from trading.entry.entry_budget import log_entry_budget_config

        log_entry_budget_config(prefix="[FAST STARTUP PATCH][ENTRY BUDGET]")
        ok = install_affordability_patch()
        logger.warning("[FAST STARTUP PATCH] entry_affordability_runtime_patch installed=%s", ok)
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry_affordability_runtime_patch install failed")


def _install_push_direct_ohlc_patch() -> None:
    try:
        from core.startup.push_summary_direct_ohlc_runtime_patch import install as install_push_direct_ohlc_patch

        ok = install_push_direct_ohlc_patch()
        logger.warning("[FAST STARTUP PATCH] push_summary_direct_ohlc_runtime_patch installed=%s", ok)
    except Exception:
        logger.exception("[FAST STARTUP PATCH] push_summary_direct_ohlc_runtime_patch install failed")


def _install_entry_max_approved_patch() -> None:
    try:
        import trading.handlers.entry_controller as ec

        old_value = getattr(ec, "MAX_APPROVED_PER_RUN", None)
        new_value = _env_int("ENTRY_MAX_APPROVED_PER_RUN", 10)
        if new_value <= 0:
            new_value = 10

        setattr(ec, "MAX_APPROVED_PER_RUN", int(new_value))

        logger.warning(
            "[FAST STARTUP PATCH] entry_controller.MAX_APPROVED_PER_RUN patched old=%s new=%s env=ENTRY_MAX_APPROVED_PER_RUN",
            old_value,
            getattr(ec, "MAX_APPROVED_PER_RUN", None),
        )
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry MAX_APPROVED_PER_RUN patch failed")


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import core.startup.scheduler_bootstrap as sb
    except Exception:
        logger.exception("[FAST STARTUP PATCH] scheduler_bootstrap import failed")
        return False

    try:
        _install_symbol_flags_bootstrap()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] symbol flags bootstrap failed")

    try:
        _patch_summary_schema_bootstrap()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] summary schema skip patch failed")

    try:
        _install_open_position_broker_merge_patch()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] open position broker patch failed")

    try:
        _install_exit_executor_broker_position_patch()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] exit executor broker patch failed")

    try:
        _install_exit_trail_03_patch()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] exit trail 0.3 patch failed")

    try:
        _install_entry_passive_limit_patch()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry passive limit patch failed")

    try:
        _install_entry_cancel_2s_next_patch()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry cancel 2s next patch failed")

    try:
        _install_entry_affordability_patch()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry affordability patch failed")

    try:
        _install_push_direct_ohlc_patch()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] push direct OHLC patch failed")

    try:
        _install_entry_max_approved_patch()
    except Exception:
        logger.exception("[FAST STARTUP PATCH] entry MAX_APPROVED_PER_RUN patch failed")

    try:
        old_lookback = getattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", None)
        new_lookback = _env_int("FAST_STARTUP_RANKING_LOOKBACK_MIN", 60)
        if new_lookback > 0:
            setattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", int(new_lookback))
        logger.warning(
            "[FAST STARTUP PATCH] ranking lookback patched old=%s new=%s env=FAST_STARTUP_RANKING_LOOKBACK_MIN",
            old_lookback,
            getattr(sb, "_DEFAULT_RANKING_LOOKBACK_MINUTES", None),
        )
    except Exception:
        logger.exception("[FAST STARTUP PATCH] lookback patch failed")

    try:
        old_job = getattr(sb, "_run_ranking_summary_all_job_safe", None)
        if callable(old_job) and not getattr(old_job, "_fast_startup_wrapped", False):

            def _ranking_job_safe_no_return(*args: Any, **kwargs: Any):
                ret = old_job(*args, **kwargs)
                try:
                    sb._set_global_attr("last_ranking_summary_job_result_type", type(ret).__name__)
                    if isinstance(ret, dict):
                        sb._set_global_attr(
                            "last_ranking_summary_job_result_summary",
                            {
                                k: {
                                    "type": type(v).__name__,
                                    "rows": len(v) if hasattr(v, "__len__") else None,
                                }
                                for k, v in ret.items()
                            },
                        )
                except Exception:
                    pass
                return None

            _ranking_job_safe_no_return._fast_startup_wrapped = True  # type: ignore[attr-defined]
            sb._run_ranking_summary_all_job_safe = _ranking_job_safe_no_return
            logger.warning("[FAST STARTUP PATCH] ranking summary scheduled job return suppressed")
    except Exception:
        logger.exception("[FAST STARTUP PATCH] ranking return suppression failed")

    try:
        skip_initial = _env_bool("FAST_STARTUP_SKIP_INITIAL_RANKING_TICK", True)
        if skip_initial:
            import __main__ as main_mod
            old_initial = getattr(main_mod, "_run_initial_ranking_tick_once", None)
            if callable(old_initial) and not getattr(old_initial, "_fast_startup_noop", False):

                def _skip_initial_ranking_tick_once():
                    logger.warning(
                        "[FAST STARTUP PATCH] initial ranking tick skipped env=FAST_STARTUP_SKIP_INITIAL_RANKING_TICK"
                    )
                    return None

                _skip_initial_ranking_tick_once._fast_startup_noop = True  # type: ignore[attr-defined]
                setattr(main_mod, "_run_initial_ranking_tick_once", _skip_initial_ranking_tick_once)
                logger.warning("[FAST STARTUP PATCH] main initial ranking tick patched to no-op")
    except Exception:
        logger.exception("[FAST STARTUP PATCH] initial ranking tick patch failed")

    _PATCHED = True
    logger.warning(
        "[FAST STARTUP PATCH] installed v11 entry_affordability=True symbol_flags_bootstrap=True push_direct_ohlc=True entry_max_approved=%s exit_trail_0p3=True entry_passive_limit=True entry_cancel_2s_next=True",
        10,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[FAST STARTUP PATCH] auto install failed")
