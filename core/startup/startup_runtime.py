# ============================================================
# File   : core/startup/startup_runtime.py
# Version: REV1.6-STARTUP-RUNTIME-RANKING-ENTRY-FIX
# ------------------------------------------------------------
# 【概要】
#   startup の runtime / engine / migration phase を分離
#
# 【主な機能】
#   - database.session の engine を動的解決
#   - engine dispose
#   - global_data.clear_all
#   - safe migration phase
#   - split mode main.py でも summary DB の履歴をメモリへ復元
#   - PUSHサマリーが最新1本だけでMACD/MTFを薄くしないよう履歴patchをinstall
#   - GlobalContext.set_merged_summary 直前にも macd/signal/mtf を履歴復元
#   - summary_controller before-cache-sync 時点で MTF / technical_ready を補正
#   - ranking entry の technical fallback / volume表示単位補正をinstall
# ============================================================

from __future__ import annotations

import importlib
import logging

from sqlalchemy.engine import Engine

from global_state import global_data
from core.startup.db_bootstrap import bootstrap_database

logger = logging.getLogger(__name__)


def _split_mode_skip_data_collector_work() -> bool:
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def resolve_engine_from_database_session(
    attr_names: tuple[str, ...],
    factory_names: tuple[str, ...],
    *,
    allow_factory: bool = True,
) -> Engine | None:
    try:
        session_mod = importlib.import_module("database.session")
    except Exception:
        logger.debug("[STARTUP.RUNTIME] database.session import failed", exc_info=True)
        return None

    for attr in attr_names:
        try:
            eng = getattr(session_mod, attr, None)
            if eng is not None:
                return eng
        except Exception:
            logger.debug("[STARTUP.RUNTIME] database.session attr engine resolve failed attr=%s", attr, exc_info=True)

    if not allow_factory:
        logger.info("[STARTUP.RUNTIME] engine factory skipped attr_names=%s split_mode=%s", attr_names, _split_mode_skip_data_collector_work())
        return None

    for fn_name in factory_names:
        try:
            fn = getattr(session_mod, fn_name, None)
            if callable(fn):
                eng = fn()
                if eng is not None:
                    return eng
        except Exception:
            logger.debug("[STARTUP.RUNTIME] database.session factory engine resolve failed fn=%s", fn_name, exc_info=True)
    return None


def resolve_summary_engine_dynamic(*, allow_factory: bool = True) -> Engine | None:
    return resolve_engine_from_database_session(
        attr_names=("summary_engine", "SUMMARY_ENGINE", "engine_summary", "summary_db_engine"),
        factory_names=("get_summary_engine", "get_engine_summary", "summary_engine_factory"),
        allow_factory=allow_factory,
    )


def resolve_ranking_engine_dynamic(*, allow_factory: bool = True) -> Engine | None:
    return resolve_engine_from_database_session(
        attr_names=("ranking_engine", "RANKING_ENGINE", "engine_ranking", "ranking_db_engine"),
        factory_names=("get_ranking_engine", "get_engine_ranking", "ranking_engine_factory"),
        allow_factory=allow_factory,
    )


def resolve_push_engine_dynamic(*, allow_factory: bool = True) -> Engine | None:
    return resolve_engine_from_database_session(
        attr_names=("push_engine", "PUSH_ENGINE", "engine_push", "push_db_engine"),
        factory_names=("get_push_engine", "get_engine_push", "push_engine_factory"),
        allow_factory=allow_factory,
    )


def dispose_all_engines() -> None:
    split = _split_mode_skip_data_collector_work()
    if split:
        logger.warning("[STARTUP.RUNTIME] split mode: skip resolving/dispose push/ranking engines in main process")
        engines = (resolve_summary_engine_dynamic(allow_factory=False),)
    else:
        engines = (resolve_summary_engine_dynamic(), resolve_ranking_engine_dynamic(), resolve_push_engine_dynamic())
    for eng in engines:
        try:
            if eng:
                eng.dispose()
        except Exception:
            pass


def clear_runtime_memory() -> None:
    try:
        global_data.clear_all()
    except Exception:
        pass


def _install_push_summary_history_patch() -> None:
    try:
        from core.startup.push_summary_history_runtime_patch import install as install_push_history
        ok = install_push_history()
        logger.warning("[STARTUP.RUNTIME] push summary history patch installed=%s", ok)
    except Exception:
        logger.exception("[STARTUP.RUNTIME] push summary history patch install failed")


def _install_global_context_summary_repair_patch() -> None:
    try:
        from core.startup.global_context_summary_repair_patch import install as install_gc_repair
        ok = install_gc_repair()
        logger.warning("[STARTUP.RUNTIME] global context summary repair patch installed=%s", ok)
    except Exception:
        logger.exception("[STARTUP.RUNTIME] global context summary repair patch install failed")


def _install_summary_controller_ready_mtf_patch() -> None:
    try:
        from core.startup.summary_controller_ready_mtf_patch import install as install_ready_mtf
        ok = install_ready_mtf()
        logger.warning("[STARTUP.RUNTIME] summary controller ready mtf patch installed=%s", ok)
    except Exception:
        logger.exception("[STARTUP.RUNTIME] summary controller ready mtf patch install failed")


def _install_ranking_entry_runtime_fix_patch() -> None:
    try:
        from core.startup.ranking_entry_runtime_fix_patch import install as install_ranking_fix
        ok = install_ranking_fix()
        logger.warning("[STARTUP.RUNTIME] ranking entry runtime fix patch installed=%s", ok)
    except Exception:
        logger.exception("[STARTUP.RUNTIME] ranking entry runtime fix patch install failed")


def _restore_summary_db_seed_after_bootstrap(summary_db_path) -> None:
    try:
        from core.startup.summary_db_seed_restore_patch import restore_summary_db_seed
        result = restore_summary_db_seed(summary_db_path)
        logger.warning("[STARTUP.RUNTIME] summary DB seed restore result=%s", result)
    except Exception:
        logger.exception("[STARTUP.RUNTIME] summary DB seed restore failed")

    _install_push_summary_history_patch()
    _install_global_context_summary_repair_patch()
    _install_summary_controller_ready_mtf_patch()
    _install_ranking_entry_runtime_fix_patch()


def safe_migration_phase(summary_dir, ranking_dir) -> None:
    logger.info("🛑 ENTER SAFE MIGRATION MODE")
    logger.info("📁 SAFE MIGRATION summary_dir=%s", summary_dir)
    logger.info("📁 SAFE MIGRATION ranking_dir=%s", ranking_dir)

    split = _split_mode_skip_data_collector_work()
    if split:
        logger.warning("[STARTUP.RUNTIME] split mode active: main.py will not create/migrate PUSH/RANKING DB. main_database.py handles DB作成 / ranking取得 / PUSH受信.")

    dispose_all_engines()
    clear_runtime_memory()

    summary_db_path = bootstrap_database(
        summary_dir,
        None if split else ranking_dir,
        skip_migration=split,
    )

    _restore_summary_db_seed_after_bootstrap(summary_db_path)
    logger.info("✅ SAFE MIGRATION COMPLETE")


__all__ = [
    "resolve_engine_from_database_session",
    "resolve_summary_engine_dynamic",
    "resolve_ranking_engine_dynamic",
    "resolve_push_engine_dynamic",
    "dispose_all_engines",
    "clear_runtime_memory",
    "safe_migration_phase",
]
