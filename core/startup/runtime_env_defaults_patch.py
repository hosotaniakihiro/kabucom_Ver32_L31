# -*- coding: utf-8 -*-
"""
Compatibility installer for centralized runtime environment defaults.

REV31:
  - install final-liquidity PUSH DB fallback after strict recent-liquidity guard,
    so stale summary DB rows do not block entries when fresh PUSH raw data proves
    recent liquidity.
REV30:
  - install summary parallel executor reset so timed-out main.py summary futures do
    not remain queued behind a single ThreadPoolExecutor worker.
  - install Tonosama orphan timeout prune so a stale timeout daemon thread does not
    permanently block later Tonosama schedule cycles.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Dict

from .runtime_env_default_registry import SITE_GROUP_ORDER, USER_GROUP_ORDER
from .runtime_env_default_registry import VERSION as REGISTRY_VERSION
from .runtime_settings_ini_loader import VERSION as SETTINGS_INI_VERSION
from .runtime_settings_ini_loader import load_settings_ini
from . import runtime_env_defaults as _defaults

logger = logging.getLogger(__name__)
VERSION = "REV31-RUNTIME-ENV-DEFAULTS-FINAL-LIQ-PUSHDB-FALLBACK"
DEFAULTS_VERSION = getattr(_defaults, "VERSION", "unknown")
env_bool = getattr(_defaults, "env_bool")
_INSTALLED = False

_GROUP_APPLIERS: dict[str, Callable[..., Dict[str, str]]] = {
    "push": getattr(_defaults, "apply_push_defaults"),
    "rescue": getattr(_defaults, "apply_rescue_defaults"),
    "db": getattr(_defaults, "apply_db_defaults"),
    "helper": getattr(_defaults, "apply_helper_defaults"),
    "main_restore": getattr(_defaults, "apply_main_restore_defaults"),
    "ranking_entry": getattr(_defaults, "apply_ranking_entry_defaults"),
    "tonosama": getattr(_defaults, "apply_tonosama_defaults"),
    "entry": getattr(_defaults, "apply_entry_defaults"),
    "summary_yahoo": getattr(_defaults, "apply_summary_yahoo_defaults"),
}

DB_CONTEXTS = {"main_database", "db_prepare", "push_receiver", "summary_database", "ranking_collector", "yahoo_complement"}
TRADING_CONTEXTS = {"main"}
GENERIC_HELPER_CONTEXTS = {"helper"}


def _apply_groups(order: tuple[str, ...], *, context: str) -> Dict[str, str]:
    applied: Dict[str, str] = {}
    for name in order:
        fn = _GROUP_APPLIERS.get(name)
        if not callable(fn):
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] group applier missing name=%s context=%s", name, context)
            continue
        try:
            applied.update(fn())
        except Exception:
            logger.exception("[RUNTIME ENV DEFAULTS PATCH] group apply failed name=%s context=%s", name, context)
    return applied


def apply_site_defaults(*, context: str = "unknown") -> Dict[str, str]:
    old = getattr(_defaults, "apply_site_defaults", None)
    if callable(old):
        return old(context=context)
    return _apply_groups(SITE_GROUP_ORDER, context=context)


def apply_user_defaults(*, context: str = "unknown") -> Dict[str, str]:
    old = getattr(_defaults, "apply_user_defaults", None)
    if callable(old):
        return old(context=context)
    return _apply_groups(USER_GROUP_ORDER, context=context)


def _argv_context() -> str:
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text:
            return "main_database"
        if text.endswith("main.py") or "/main.py" in text:
            return "main"
        if "db_prepare_runner.py" in text:
            return "db_prepare"
        if "push_receiver_runner.py" in text:
            return "push_receiver"
        if "summary_database_runner.py" in text:
            return "summary_database"
        if "ranking_collector_runner.py" in text:
            return "ranking_collector"
        if "yahoo_complement_runner.py" in text:
            return "yahoo_complement"
        if "data_collectors_runner.py" in text:
            return "main_database"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _safe_install(label: str, context: str, allowed: set[str], env_disable: str, module_name: str) -> bool:
    try:
        if context not in allowed:
            return False
        if os.environ.get(env_disable, "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] %s disabled by env", label)
            return False
        mod = __import__(f"core.startup.{module_name}", fromlist=["install"])
        fn = getattr(mod, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[RUNTIME ENV DEFAULTS PATCH] %s install ok=%s context=%s", label, ok, context)
        return ok
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] %s install failed", label)
        return False


def _apply_ranking_api_spacing_default(context: str) -> Dict[str, str]:
    applied: Dict[str, str] = {}
    try:
        if context in {"main_database", "ranking_collector"}:
            if os.getenv("RANKING_API_CALL_SLEEP_SEC") is None or str(os.getenv("RANKING_API_CALL_SLEEP_SEC")).strip() == "":
                os.environ["RANKING_API_CALL_SLEEP_SEC"] = "0.5"
                applied["RANKING_API_CALL_SLEEP_SEC"] = "0.5"
            if os.getenv("RANKING_API_SPREAD_OVER_MINUTE") is None or str(os.getenv("RANKING_API_SPREAD_OVER_MINUTE")).strip() == "":
                os.environ["RANKING_API_SPREAD_OVER_MINUTE"] = "0"
                applied["RANKING_API_SPREAD_OVER_MINUTE"] = "0"
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] ranking api spacing default failed context=%s", context)
    return applied


def _install_strict_entry_defaults(context: str) -> bool:
    try:
        if context not in TRADING_CONTEXTS:
            return False
        if os.environ.get("DISABLE_STRICT_ENTRY_DEFAULTS_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] strict entry defaults disabled by env")
            return False
        from . import tonosama_history_failclose_strict_patch
        ok = bool(tonosama_history_failclose_strict_patch.install())
        logger.warning("[RUNTIME ENV DEFAULTS PATCH] strict entry defaults installed ok=%s context=%s", ok, context)
        return ok
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] strict entry defaults install failed")
        return False


def _force_install_summary_ai_safety_guard(context: str) -> bool:
    try:
        if context not in TRADING_CONTEXTS:
            return False
        if os.environ.get("DISABLE_SUMMARY_AI_SAFETY_GUARD", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] summary AI safety guard disabled by env")
            return False
        os.environ.setdefault("SUMMARY_AI_REQUIRE_PUSH_WRITER_READY", "1")
        os.environ.setdefault("SUMMARY_AI_REQUIRE_FRESH_PUSH_1M", "1")
        os.environ.setdefault("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", "120")
        os.environ.setdefault("SUMMARY_STALE_DB_FALLBACK_MAX_AGE_SEC", "420")
        os.environ.setdefault("TONOSAMA_DISABLE_HISTORY_FAILOPEN", "1")
        os.environ.setdefault("TONOSAMA_REQUIRE_TECHNICAL_READY", "1")
        from . import summary_ai_candidate_refill_patch
        ok = bool(summary_ai_candidate_refill_patch.install())
        logger.warning("[RUNTIME ENV DEFAULTS PATCH] forced SUMMARY AI SAFETY GUARD ok=%s context=%s", ok, context)
        return ok
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] forced SUMMARY AI SAFETY GUARD install failed")
        return False


def _install_summary_ai_direct_timeout_continue(context: str) -> bool:
    return _safe_install("summary AI direct timeout continue", context, TRADING_CONTEXTS, "DISABLE_SUMMARY_AI_DIRECT_TIMEOUT_CONTINUE_PATCH", "summary_ai_direct_timeout_continue_patch")


def _install_summary_ai_atr_1m_filter_repair(context: str) -> bool:
    return _safe_install("summary AI ATR 1m filter repair", context, TRADING_CONTEXTS, "DISABLE_SUMMARY_AI_ATR_1M_FILTER_REPAIR_PATCH", "summary_ai_atr_1m_filter_repair_patch")


def _install_summary_parallel_executor_reset(context: str) -> bool:
    return _safe_install("summary parallel executor reset", context, TRADING_CONTEXTS, "DISABLE_SUMMARY_PARALLEL_EXECUTOR_RESET_PATCH", "summary_parallel_executor_reset_patch")


def _install_tonosama_orphan_timeout_prune(context: str) -> bool:
    return _safe_install("tonosama orphan timeout prune", context, TRADING_CONTEXTS, "DISABLE_TONOSAMA_ORPHAN_TIMEOUT_PRUNE_PATCH", "tonosama_orphan_timeout_prune_patch")


def _install_entry_fire_rescue(context: str) -> bool:
    return _safe_install("entry fire rescue", context, TRADING_CONTEXTS, "DISABLE_ENTRY_FIRE_RESCUE_PATCH", "entry_fire_rescue_runtime_patch")


def _install_summary_pending_stale_guard(context: str) -> bool:
    try:
        if context not in TRADING_CONTEXTS:
            return False
        if os.environ.get("DISABLE_SUMMARY_ENTRY_PENDING_FIX_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] summary pending stale guard disabled by env")
            return False
        os.environ.setdefault("SUMMARY_SKIP_STALE_PENDING_ENABLED", "1")
        os.environ.setdefault("SUMMARY_SKIP_MISSING_DATETIME_PENDING", "1")
        os.environ.setdefault("SUMMARY_ENTRY_PENDING_MAX_AGE_SEC", "180")
        from . import summary_entry_pending_existing_fix_patch
        return bool(summary_entry_pending_existing_fix_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] summary pending stale guard install failed")
        return False


def _install_summary_db_realtime_priority(context: str) -> bool:
    return _safe_install("summary DB realtime priority", context, DB_CONTEXTS, "DISABLE_SUMMARY_DB_REALTIME_PRIORITY_PATCH", "summary_db_realtime_priority_patch")


def _install_push_summary_db_latest_source(context: str) -> bool:
    return _safe_install("push summary DB latest source", context, DB_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_PUSH_SUMMARY_DB_SOURCE_PATCH", "push_summary_db_latest_source_patch")


def _install_ranking_entry_runtime_rescue(context: str) -> bool:
    return _safe_install("ranking entry runtime rescue", context, TRADING_CONTEXTS, "DISABLE_RANKING_ENTRY_RUNTIME_RESCUE_PATCH", "ranking_entry_runtime_rescue_patch")


def _install_low_volatility_entry_guard(context: str) -> bool:
    try:
        if context not in TRADING_CONTEXTS:
            return False
        if os.environ.get("DISABLE_LOW_VOLATILITY_ENTRY_GUARD", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] low volatility entry guard disabled by env")
            return False
        from . import low_volatility_entry_guard_patch
        base_ok = bool(low_volatility_entry_guard_patch.install())
        strict_ok = False
        try:
            from . import low_volatility_entry_guard_strict_patch
            strict_ok = bool(low_volatility_entry_guard_strict_patch.install())
        except Exception:
            logger.exception("[RUNTIME ENV DEFAULTS PATCH] strict low volatility guard install failed")
        return bool(base_ok or strict_ok)
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] low volatility entry guard install failed")
        return False


def _install_push_registration_recovery(context: str) -> bool:
    return _safe_install("push register recovery", context, {"main", "main_database", "push_receiver"}, "DISABLE_PUSH_REGISTER_RECOVERY_PATCH", "push_registration_recovery_patch")


def _install_common_day_position_guard(context: str) -> bool:
    return _safe_install("common day position guard", context, TRADING_CONTEXTS, "DISABLE_COMMON_ENTRY_DAY_POSITION_GUARD", "common_entry_day_position_guard_patch")


def _install_strict_final_liquidity_guard(context: str) -> bool:
    return _safe_install("strict final liquidity guard", context, TRADING_CONTEXTS, "DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PATCH", "entry_handler_strict_recent_liquidity_patch")


def _install_strict_final_liq_pushdb_fallback(context: str) -> bool:
    return _safe_install("strict final liquidity pushdb fallback", context, TRADING_CONTEXTS, "DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PUSHDB_FALLBACK_PATCH", "entry_handler_strict_recent_liquidity_pushdb_fallback_patch")


def _install_tonosama_exit_source_infer(context: str) -> bool:
    return _safe_install("tonosama exit infer", context, TRADING_CONTEXTS, "DISABLE_TONOSAMA_EXIT_SOURCE_INFER_PATCH", "tonosama_exit_source_infer_patch")


def _install_tonosama_pending_candidate_audit(context: str) -> bool:
    return _safe_install("tonosama pending candidate audit", context, TRADING_CONTEXTS, "DISABLE_TONOSAMA_PENDING_CANDIDATE_AUDIT_PATCH", "tonosama_pending_candidate_audit_patch")


def _install_daytrade_credit_force_close(context: str) -> bool:
    return _safe_install("daytrade credit force close", context, TRADING_CONTEXTS, "DISABLE_DAYTRADE_CREDIT_FORCE_CLOSE_PATCH", "daytrade_credit_force_close_patch")


def _install_database_owner(context: str) -> bool:
    return _safe_install("database owner", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_DATABASE_OWNER_RUNTIME_PATCH", "database_owner_runtime_patch")


def _install_entry_count_unblock(context: str) -> bool:
    return _safe_install("entry count unblock", context, TRADING_CONTEXTS | {"main_database"}, "DISABLE_ENTRY_COUNT_UNBLOCK_PATCH", "entry_count_unblock_runtime_patch")


def _install_full_pipeline_stability(context: str) -> bool:
    return _safe_install("full pipeline stability", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_FULL_PIPELINE_STABILITY_PATCH", "full_pipeline_stability_runtime_patch")


def _install_summary_db_lock_pressure(context: str) -> bool:
    return _safe_install("summary DB lock pressure", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_SUMMARY_DB_LOCK_PRESSURE_PATCH", "summary_db_lock_pressure_patch")


def _install_intraday_load_guard(context: str) -> bool:
    return _safe_install("intraday load guard", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_INTRADAY_LOAD_GUARD_PATCH", "intraday_load_guard_patch")


def _install_yahoo_parallel_empty_cooldown(context: str) -> bool:
    return _safe_install("yahoo parallel empty cooldown", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_YAHOO_PARALLEL_EMPTY_COOLDOWN_PATCH", "yahoo_parallel_empty_cooldown_patch")


def _install_ranking_legacy_inline_flush(context: str) -> bool:
    return _safe_install("ranking legacy inline flush", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_RANKING_LEGACY_INLINE_FLUSH_PATCH", "ranking_legacy_inline_flush_patch")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        context = _argv_context()
        settings_applied: Dict[str, str] = load_settings_ini(context=context)
        applied: Dict[str, str] = {}
        applied.update(apply_site_defaults(context=context))
        applied.update(apply_user_defaults(context=context))
        ranking_spacing_applied = _apply_ranking_api_spacing_default(context)
        applied.update(ranking_spacing_applied)

        strict_entry_defaults_ok = _install_strict_entry_defaults(context)
        intraday_load_guard_ok = _install_intraday_load_guard(context)
        yahoo_parallel_empty_ok = _install_yahoo_parallel_empty_cooldown(context)
        ranking_legacy_inline_ok = _install_ranking_legacy_inline_flush(context)
        summary_lock_pressure_ok = _install_summary_db_lock_pressure(context)
        push_summary_db_source_ok = _install_push_summary_db_latest_source(context)
        summary_db_realtime_ok = _install_summary_db_realtime_priority(context)
        database_owner_ok = _install_database_owner(context)
        full_pipeline_ok = _install_full_pipeline_stability(context)
        entry_count_unblock_ok = _install_entry_count_unblock(context)
        summary_ai_safety_ok = _force_install_summary_ai_safety_guard(context)
        summary_ai_direct_timeout_continue_ok = _install_summary_ai_direct_timeout_continue(context)
        summary_ai_atr_1m_repair_ok = _install_summary_ai_atr_1m_filter_repair(context)
        summary_parallel_reset_ok = _install_summary_parallel_executor_reset(context)
        tonosama_orphan_prune_ok = _install_tonosama_orphan_timeout_prune(context)
        summary_pending_stale_ok = _install_summary_pending_stale_guard(context)
        entry_fire_rescue_ok = _install_entry_fire_rescue(context)
        ranking_entry_rescue_ok = _install_ranking_entry_runtime_rescue(context)
        low_vol_guard_ok = _install_low_volatility_entry_guard(context)
        push_register_recovery_ok = _install_push_registration_recovery(context)
        day_position_guard_ok = _install_common_day_position_guard(context)
        strict_final_liq_ok = _install_strict_final_liquidity_guard(context)
        strict_final_liq_pushdb_ok = _install_strict_final_liq_pushdb_fallback(context)
        tonosama_exit_infer_ok = _install_tonosama_exit_source_infer(context)
        tonosama_pending_audit_ok = _install_tonosama_pending_candidate_audit(context)
        daytrade_credit_ok = _install_daytrade_credit_force_close(context)
        _INSTALLED = True
        logger.warning(
            "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s settings_ini=%s settings_applied=%s builtins_applied=%s context=%s site_groups=%s user_groups=%s ranking_api_sleep=%s ranking_spacing_applied=%s strict_entry_defaults=%s intraday_load_guard=%s yahoo_parallel_empty=%s ranking_legacy_inline=%s summary_lock_pressure=%s push_summary_db_source=%s summary_db_realtime=%s database_owner=%s full_pipeline=%s rescue=%s ranking_rescue=%s tonosama_rescue=%s entry_count_unblock=%s summary_ai_safety=%s summary_ai_direct_timeout_continue=%s summary_ai_atr_1m_repair=%s summary_parallel_reset=%s tonosama_orphan_prune=%s summary_pending_stale=%s entry_fire_rescue=%s ranking_entry_rescue=%s low_vol_guard=%s push_register_recovery=%s day_position_guard=%s strict_final_liq=%s strict_final_liq_pushdb=%s tonosama_exit_infer=%s tonosama_pending_audit=%s daytrade_credit=%s verbose=%s",
            VERSION, DEFAULTS_VERSION, REGISTRY_VERSION, SETTINGS_INI_VERSION, len(settings_applied), len(applied), context,
            ",".join(SITE_GROUP_ORDER), ",".join(USER_GROUP_ORDER), os.environ.get("RANKING_API_CALL_SLEEP_SEC"), ranking_spacing_applied,
            strict_entry_defaults_ok, intraday_load_guard_ok, yahoo_parallel_empty_ok, ranking_legacy_inline_ok, summary_lock_pressure_ok,
            push_summary_db_source_ok, summary_db_realtime_ok, database_owner_ok, full_pipeline_ok, os.environ.get("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES"),
            os.environ.get("USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES"), os.environ.get("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES"),
            entry_count_unblock_ok, summary_ai_safety_ok, summary_ai_direct_timeout_continue_ok, summary_ai_atr_1m_repair_ok,
            summary_parallel_reset_ok, tonosama_orphan_prune_ok, summary_pending_stale_ok, entry_fire_rescue_ok, ranking_entry_rescue_ok,
            low_vol_guard_ok, push_register_recovery_ok, day_position_guard_ok, strict_final_liq_ok, strict_final_liq_pushdb_ok,
            tonosama_exit_infer_ok, tonosama_pending_audit_ok, daytrade_credit_ok, env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False),
        )
        return True
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] install failed")
        return False


__all__ = [
    "VERSION", "install", "apply_site_defaults", "apply_user_defaults",
]
