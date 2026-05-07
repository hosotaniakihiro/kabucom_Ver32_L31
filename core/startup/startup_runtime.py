# ============================================================
# File   : core/startup/startup_runtime.py
# Version: REV1.1-STARTUP-RUNTIME-SPLIT-MODE
# ------------------------------------------------------------
# 【概要】
#   startup の runtime / engine / migration phase を分離
#
# 【主な機能】
#   - database.session の engine を動的解決
#   - engine dispose
#   - global_data.clear_all
#   - safe migration phase
#
# Split mode:
#   - main_database.py が DB作成 / ranking取得 / PUSH受信を担当
#   - main.py 側では push/ranking engine の factory を呼ばない
#   - main.py 側では DB migration を走らせず、summary engine の解決だけ行う
#
# 【重要】
#   from database.session import summary_engine の固定参照は使わない。
#   bootstrap_database() 後に database.session 側で rebind される可能性があるため、
#   必ず importlib で最新 module から engine を取り直す。
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
            logger.debug(
                "[STARTUP.RUNTIME] database.session attr engine resolve failed attr=%s",
                attr,
                exc_info=True,
            )

    if not allow_factory:
        logger.info(
            "[STARTUP.RUNTIME] engine factory skipped attr_names=%s split_mode=%s",
            attr_names,
            _split_mode_skip_data_collector_work(),
        )
        return None

    for fn_name in factory_names:
        try:
            fn = getattr(session_mod, fn_name, None)
            if callable(fn):
                eng = fn()
                if eng is not None:
                    return eng
        except Exception:
            logger.debug(
                "[STARTUP.RUNTIME] database.session factory engine resolve failed fn=%s",
                fn_name,
                exc_info=True,
            )

    return None


def resolve_summary_engine_dynamic(*, allow_factory: bool = True) -> Engine | None:
    return resolve_engine_from_database_session(
        attr_names=(
            "summary_engine",
            "SUMMARY_ENGINE",
            "engine_summary",
            "summary_db_engine",
        ),
        factory_names=(
            "get_summary_engine",
            "get_engine_summary",
            "summary_engine_factory",
        ),
        allow_factory=allow_factory,
    )


def resolve_ranking_engine_dynamic(*, allow_factory: bool = True) -> Engine | None:
    return resolve_engine_from_database_session(
        attr_names=(
            "ranking_engine",
            "RANKING_ENGINE",
            "engine_ranking",
            "ranking_db_engine",
        ),
        factory_names=(
            "get_ranking_engine",
            "get_engine_ranking",
            "ranking_engine_factory",
        ),
        allow_factory=allow_factory,
    )


def resolve_push_engine_dynamic(*, allow_factory: bool = True) -> Engine | None:
    return resolve_engine_from_database_session(
        attr_names=(
            "push_engine",
            "PUSH_ENGINE",
            "engine_push",
            "push_db_engine",
        ),
        factory_names=(
            "get_push_engine",
            "get_engine_push",
            "push_engine_factory",
        ),
        allow_factory=allow_factory,
    )


def dispose_all_engines() -> None:
    split = _split_mode_skip_data_collector_work()

    if split:
        logger.warning(
            "[STARTUP.RUNTIME] split mode: skip resolving/dispose push/ranking engines in main process"
        )
        engines = (
            resolve_summary_engine_dynamic(allow_factory=False),
        )
    else:
        engines = (
            resolve_summary_engine_dynamic(),
            resolve_ranking_engine_dynamic(),
            resolve_push_engine_dynamic(),
        )

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


def safe_migration_phase(summary_dir, ranking_dir) -> None:
    logger.info("🛑 ENTER SAFE MIGRATION MODE")
    logger.info("📁 SAFE MIGRATION summary_dir=%s", summary_dir)
    logger.info("📁 SAFE MIGRATION ranking_dir=%s", ranking_dir)

    split = _split_mode_skip_data_collector_work()
    if split:
        logger.warning(
            "[STARTUP.RUNTIME] split mode active: main.py will not create/migrate PUSH/RANKING DB. "
            "main_database.py handles DB作成 / ranking取得 / PUSH受信."
        )

    dispose_all_engines()
    clear_runtime_memory()

    bootstrap_database(
        summary_dir,
        None if split else ranking_dir,
        skip_migration=split,
    )
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