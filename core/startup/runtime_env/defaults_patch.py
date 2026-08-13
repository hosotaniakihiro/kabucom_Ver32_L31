# -*- coding: utf-8 -*-
"""
Compatibility installer for centralized runtime environment defaults.

REV33:
  - install volatility_filter ranking rescue timebox so SUMMARY_AI direct snapshot
    does not hang while scanning stale ranking DB files on NAS.
REV32:
  - install normal EXIT scalp alignment so SUMMARY_AI / RANKING normal exits use
    the same quick take/trailing/timeout parameters as Tonosama/Inago exits.
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

from .registry import SITE_GROUP_ORDER, USER_GROUP_ORDER
from .registry import VERSION as REGISTRY_VERSION
from .settings_loader import VERSION as SETTINGS_INI_VERSION
from .settings_loader import load_settings_ini
from . import defaults as _defaults

logger = logging.getLogger(__name__)
VERSION = "REV33-RUNTIME-ENV-DEFAULTS-VOL-FILTER-RESCUE-TIMEBOX"
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
        from .. import tonosama_history_failclose_strict_patch
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
        from .. import summary_ai_candidate_refill_patch
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
    # tonosama_orphan_timeout_prune_patch は trading/entry_exit/tasks.py の
    # start_tonosama_orphan_prune_watcher() へ native化済み。
    try:
        if context not in TRADING_CONTEXTS:
            return False
        if os.environ.get("DISABLE_TONOSAMA_ORPHAN_TIMEOUT_PRUNE_PATCH", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] tonosama orphan timeout prune disabled by env")
            return False
        from trading.entry_exit.tasks import start_tonosama_orphan_prune_watcher

        ok = bool(start_tonosama_orphan_prune_watcher())
        logger.warning("[RUNTIME ENV DEFAULTS PATCH] tonosama orphan timeout prune install ok=%s context=%s", ok, context)
        return ok
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] tonosama orphan timeout prune install failed")
        return False


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
        from .. import summary_entry_pending_existing_fix_patch
        return bool(summary_entry_pending_existing_fix_patch.install())
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] summary pending stale guard install failed")
        return False


def _install_summary_db_realtime_priority(context: str) -> bool:
    # summary_db_realtime_priority_patch は scheduler_jobs/summary/time_utils.py の
    # resolve_target_intervals() へ native化済み (_summary_db_realtime_priority_enabled()
    # がここと同じ DB_CONTEXTS 相当のargv/env判定を再現し、module import時にenvデフォルトも
    # 適用済み)。ここでは有効/無効の状態をログへ反映するためだけに判定結果を返す。
    if context not in DB_CONTEXTS:
        return False
    if os.environ.get("DISABLE_SUMMARY_DB_REALTIME_PRIORITY_PATCH", "").strip() == "1":
        return False
    if not env_bool("SUMMARY_DB_REALTIME_PRIORITY_ENABLED", True):
        return False
    return True


def _install_push_summary_db_latest_source(context: str) -> bool:
    # push_summary_db_latest_source_patch は trading/summary/engine/push_summary_engine.py の
    # _resolve_push_source_df() へ native化済み (_push_summary_db_latest_source_scope_enabled()
    # がここと同じ DB_CONTEXTS|GENERIC_HELPER_CONTEXTS 判定を再現する)。ここでは有効/無効の
    # 状態をログへ反映するためだけに判定結果を返す。
    if context not in (DB_CONTEXTS | GENERIC_HELPER_CONTEXTS):
        return False
    if os.environ.get("DISABLE_PUSH_SUMMARY_DB_SOURCE_PATCH", "").strip() == "1":
        return False
    if not env_bool("PUSH_SUMMARY_DB_SOURCE_ENABLED", True):
        return False
    return True


def _install_ranking_entry_runtime_rescue(context: str) -> bool:
    # ranking entry runtime rescue (レンジ0行の high/low 補完) は
    # trading/ranking/ranking_entry_high_low._needs_high_low へ本文化済み。
    # 適用スコープ (context=main / DISABLE_RANKING_ENTRY_RUNTIME_RESCUE_PATCH /
    # RANKING_ENTRY_RUNTIME_RESCUE_ENABLED) も同関数側で再現している。
    return context in TRADING_CONTEXTS


def _install_low_volatility_entry_guard(context: str) -> bool:
    try:
        if context not in TRADING_CONTEXTS:
            return False
        if os.environ.get("DISABLE_LOW_VOLATILITY_ENTRY_GUARD", "").strip() == "1":
            logger.warning("[RUNTIME ENV DEFAULTS PATCH] low volatility entry guard disabled by env")
            return False
        # low_volatility_entry_guard_patch は削除した (完全に不活性だったため)。
        #   - _patch_ai_entry_gate      : AI/entry_gate.py へ本文化済みで no-op
        #   - _patch_summary_ai_selection: executor の core filter watcher に
        #                                  剥がされる死にラッパで撤去済み
        #   - _patch_entry_order_builder_low_move:
        #       _low_move_hard_block は install 順で summary_ai_range_consistency に
        #       必ず上書きされる死にラッパだったため撤去 (本文化時に確認)
        #   - 残っていた os.environ.setdefault("ENTRY_LOW_VOL_ZERO_AS_MISSING","1") も、
        #     唯一の実効読み手 AI/entry_gate.py:780 が既定 True で読むため実効ゼロ
        # 低ボラガード本体は AI/entry_gate.py (_low_vol_block 等) が持っている。
        return True
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] low volatility entry guard install failed")
        return False


def _install_vol_filter_ranking_rescue_timebox(context: str) -> bool:
    # volatility_filter_ranking_rescue_timebox_patch は trading/filters/volatility_filter.py の
    # _latest_ranking_snapshot_move() へ native化済み (_vol_filter_rescue_timebox_scope_enabled()
    # がここと同じ TRADING_CONTEXTS={"main"} + 両env判定を再現する)。ここでは有効/無効の
    # 状態をログへ反映するためだけに判定結果を返す。
    if context not in TRADING_CONTEXTS:
        return False
    if os.environ.get("DISABLE_VOL_FILTER_RANKING_RESCUE_TIMEBOX_PATCH", "").strip() == "1":
        return False
    if not env_bool("VOL_FILTER_RANKING_RESCUE_TIMEBOX_ENABLED", True):
        return False
    return True


def _install_push_registration_recovery(context: str) -> bool:
    return _safe_install("push register recovery", context, {"main", "main_database", "push_receiver"}, "DISABLE_PUSH_REGISTER_RECOVERY_PATCH", "push_registration_recovery_patch")


def _install_common_day_position_guard(context: str) -> bool:
    # common_entry_day_position_guard_patch と pending_protect_push_patch は
    # trading/entry/pending_manager.add_pending へ2層まとめて本文化済み
    # (native <- common_day_position_guard <- pending_protect_push の順序を保持)。
    return context in TRADING_CONTEXTS


def _install_strict_final_liquidity_guard(context: str) -> bool:
    return _safe_install("strict final liquidity guard", context, TRADING_CONTEXTS, "DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PATCH", "entry_handler_strict_recent_liquidity_patch")


def _install_strict_final_liq_pushdb_fallback(context: str) -> bool:
    """旧 entry_handler_strict_recent_liquidity_pushdb_fallback_patch の導線。

    同パッチは削除した。ラップ対象の
    entry_handler_strict_recent_liquidity_patch._global_context_liquidity_values は
    リポジトリのどこにも定義が無く、install() が
    `if not callable(current): return False` で必ず抜けていたため、
    PUSH DB フォールバックは一度も適用されていなかった。

    実効のあった唯一の副作用は install() 冒頭の base.install() 呼び出しなので、
    ゲート名 (DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PUSHDB_FALLBACK_PATCH) と
    スコープをそのままに、base を直接 install する形へ置き換えた。
    上の _install_strict_final_liq_guard と二重になるが、install() は
    _INSTALLED ガードで冪等。片方だけ env で無効化された場合の挙動も従来どおり。
    """
    return _safe_install("strict final liquidity guard (via pushdb fallback route)", context, TRADING_CONTEXTS, "DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PUSHDB_FALLBACK_PATCH", "entry_handler_strict_recent_liquidity_patch")


def _install_tonosama_exit_source_infer(context: str) -> bool:
    # tonosama_exit_source_infer_patch は trading/handlers/exit_handler.py の
    # _get()/TONO_* 定数へ native化済み (_tonosama_exit_infer_scope_enabled() が
    # ここと同じ TRADING_CONTEXTS={"main"} 相当のargv判定を module import 時に再現する)。
    # ここでは有効/無効の状態をログへ反映するためだけに判定結果を返す。
    if context not in TRADING_CONTEXTS:
        return False
    if os.environ.get("DISABLE_TONOSAMA_EXIT_SOURCE_INFER_PATCH", "").strip() == "1":
        return False
    return True


def _install_normal_exit_scalp_align(context: str) -> bool:
    return _safe_install("normal exit scalp align", context, TRADING_CONTEXTS, "DISABLE_NORMAL_EXIT_SCALP_ALIGN_PATCH", "normal_exit_scalp_align_patch")


def _install_tonosama_pending_candidate_audit(context: str) -> bool:
    # tonosama_pending_candidate_audit_patch は trading/entry/tonosama/pending_writer.py の
    # add_tonosama_pending() へ native化済み (scope判定は _pending_audit_scope_enabled()
    # がここと同じ TRADING_CONTEXTS={"main"} 相当のargv判定を再現する)。ここでは有効/無効の
    # 状態をログへ反映するためだけに判定結果を返す。
    if context not in TRADING_CONTEXTS:
        return False
    if os.environ.get("DISABLE_TONOSAMA_PENDING_CANDIDATE_AUDIT_PATCH", "").strip() == "1":
        return False
    return True


def _install_daytrade_credit_force_close(context: str) -> bool:
    return _safe_install("daytrade credit force close", context, TRADING_CONTEXTS, "DISABLE_DAYTRADE_CREDIT_FORCE_CLOSE_PATCH", "daytrade_credit_force_close_patch")


def _install_database_owner(context: str) -> bool:
    return _safe_install("database owner", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_DATABASE_OWNER_RUNTIME_PATCH", "database_owner_runtime_patch")


def _install_entry_count_unblock(context: str) -> bool:
    return _safe_install("entry count unblock", context, TRADING_CONTEXTS | {"main_database"}, "DISABLE_ENTRY_COUNT_UNBLOCK_PATCH", "entry_count_unblock_runtime_patch")


def _install_full_pipeline_stability(context: str) -> bool:
    return _safe_install("full pipeline stability", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_FULL_PIPELINE_STABILITY_PATCH", "full_pipeline_stability_runtime_patch")


def _install_summary_db_lock_pressure(context: str) -> bool:
    # summary_db_lock_pressure_patch は
    # trading/summary/persistence/summary_save_guards.py へ集約済み
    # (同パッケージの __init__.py が enrichment の後に install する)。
    # install() が setdefault していた env は runtime_env_defaults.DB_DEFAULTS へ移した。
    # 無効化は従来どおり DISABLE_SUMMARY_DB_LOCK_PRESSURE_PATCH で行える。
    return True


def _install_intraday_load_guard(context: str) -> bool:
    return _safe_install("intraday load guard", context, DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS, "DISABLE_INTRADAY_LOAD_GUARD_PATCH", "intraday_load_guard_patch")


def _install_yahoo_parallel_empty_cooldown(context: str) -> bool:
    # yahoo_parallel_empty_cooldown_patch は trading/yahoo/yahoo_parallel_fetch.py の
    # parallel_fetch_symbols() へ native化済み (_yahoo_parallel_empty_cooldown_scope_enabled()
    # がここと同じ DB_CONTEXTS|TRADING_CONTEXTS|GENERIC_HELPER_CONTEXTS 相当の判定を再現する)。
    # ここでは有効/無効の状態をログへ反映するためだけに判定結果を返す。
    if context not in (DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS):
        return False
    if os.environ.get("DISABLE_YAHOO_PARALLEL_EMPTY_COOLDOWN_PATCH", "").strip() == "1":
        return False
    return True


_RANKING_LEGACY_FLUSH_CONTEXTS = DB_CONTEXTS | TRADING_CONTEXTS | GENERIC_HELPER_CONTEXTS


def _install_ranking_legacy_inline_flush(context: str) -> bool:
    """旧 core/startup/ranking_legacy_inline_flush_patch.py を畳んだもの。

    あのファイルは自称 compatibility wrapper で、env を 8 件 setdefault してから
    ranking_writer_lock_buffer_relief_patch を install するだけの中継だった
    (モジュール属性の書き換えはゼロ)。唯一の呼び出し元がここだったので直接呼ぶ。
    env 適用のタイミングは旧構成と同じ「context 一致かつ DISABLE でない場合のみ、
    relief patch の install 直前」に揃えてある。
    """
    if context not in _RANKING_LEGACY_FLUSH_CONTEXTS:
        return False
    if os.environ.get("DISABLE_RANKING_LEGACY_INLINE_FLUSH_PATCH", "").strip() == "1":
        logger.warning("[RUNTIME ENV DEFAULTS PATCH] ranking legacy inline flush disabled by env")
        return False
    try:
        os.environ.setdefault("RANKING_WRITER_DISABLE_LEGACY_ON_LOCK_RELIEF", "1")
        os.environ.setdefault("RANKING_WRITER_RETRY_KEEP_LEGACY", "0")
        os.environ.setdefault("RANKING_WRITER_RETRY_LEGACY_MAX_ROWS", "0")
        os.environ.setdefault("RANKING_WRITER_RETRY_RAW_MAX_ROWS", "3000")
        os.environ.setdefault("RANKING_WRITER_RETRY_SNAPSHOT_MAX_ROWS", "3000")
        os.environ.setdefault("RANKING_WRITER_BUFFER_SIZE", "1000")
        os.environ.setdefault("RANKING_WRITER_FLUSH_ON_THRESHOLD", "0")
        os.environ.setdefault("RANKING_DB_WRITER_BUSY_TIMEOUT_MS", "30000")
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] ranking legacy inline flush env failed")
    return _safe_install(
        "ranking writer lock buffer relief", context, _RANKING_LEGACY_FLUSH_CONTEXTS,
        "DISABLE_RANKING_LEGACY_INLINE_FLUSH_PATCH", "ranking_writer_lock_buffer_relief_patch",
    )


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
        vol_filter_rescue_timebox_ok = _install_vol_filter_ranking_rescue_timebox(context)
        push_register_recovery_ok = _install_push_registration_recovery(context)
        day_position_guard_ok = _install_common_day_position_guard(context)
        strict_final_liq_ok = _install_strict_final_liquidity_guard(context)
        strict_final_liq_pushdb_ok = _install_strict_final_liq_pushdb_fallback(context)
        tonosama_exit_infer_ok = _install_tonosama_exit_source_infer(context)
        normal_exit_scalp_ok = _install_normal_exit_scalp_align(context)
        tonosama_pending_audit_ok = _install_tonosama_pending_candidate_audit(context)
        daytrade_credit_ok = _install_daytrade_credit_force_close(context)
        _INSTALLED = True
        logger.warning(
            "[RUNTIME ENV DEFAULTS PATCH] installed version=%s defaults=%s registry=%s settings_ini=%s settings_applied=%s builtins_applied=%s context=%s site_groups=%s user_groups=%s ranking_api_sleep=%s ranking_spacing_applied=%s strict_entry_defaults=%s intraday_load_guard=%s yahoo_parallel_empty=%s ranking_legacy_inline=%s summary_lock_pressure=%s push_summary_db_source=%s summary_db_realtime=%s database_owner=%s full_pipeline=%s rescue=%s ranking_rescue=%s tonosama_rescue=%s entry_count_unblock=%s summary_ai_safety=%s summary_ai_direct_timeout_continue=%s summary_ai_atr_1m_repair=%s summary_parallel_reset=%s tonosama_orphan_prune=%s summary_pending_stale=%s entry_fire_rescue=%s ranking_entry_rescue=%s low_vol_guard=%s vol_filter_rescue_timebox=%s push_register_recovery=%s day_position_guard=%s strict_final_liq=%s strict_final_liq_pushdb=%s tonosama_exit_infer=%s normal_exit_scalp=%s tonosama_pending_audit=%s daytrade_credit=%s verbose=%s",
            VERSION, DEFAULTS_VERSION, REGISTRY_VERSION, SETTINGS_INI_VERSION, len(settings_applied), len(applied), context,
            ",".join(SITE_GROUP_ORDER), ",".join(USER_GROUP_ORDER), os.environ.get("RANKING_API_CALL_SLEEP_SEC"), ranking_spacing_applied,
            strict_entry_defaults_ok, intraday_load_guard_ok, yahoo_parallel_empty_ok, ranking_legacy_inline_ok, summary_lock_pressure_ok,
            push_summary_db_source_ok, summary_db_realtime_ok, database_owner_ok, full_pipeline_ok, os.environ.get("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES"),
            os.environ.get("USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES"), os.environ.get("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES"),
            entry_count_unblock_ok, summary_ai_safety_ok, summary_ai_direct_timeout_continue_ok, summary_ai_atr_1m_repair_ok,
            summary_parallel_reset_ok, tonosama_orphan_prune_ok, summary_pending_stale_ok, entry_fire_rescue_ok, ranking_entry_rescue_ok,
            low_vol_guard_ok, vol_filter_rescue_timebox_ok, push_register_recovery_ok, day_position_guard_ok, strict_final_liq_ok, strict_final_liq_pushdb_ok,
            tonosama_exit_infer_ok, normal_exit_scalp_ok, tonosama_pending_audit_ok, daytrade_credit_ok, env_bool("RUNTIME_ENV_DEFAULTS_VERBOSE", False),
        )
        return True
    except Exception:
        logger.exception("[RUNTIME ENV DEFAULTS PATCH] install failed")
        return False


__all__ = [
    "VERSION", "install", "apply_site_defaults", "apply_user_defaults",
]
