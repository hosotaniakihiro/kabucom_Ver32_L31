# ============================================================
# File   : database/__init__.py
# Version: Ver02-REGISTER-SUMMARY-STATE-SCHEMA
# ------------------------------------------------------------
# database.session を公開しつつ、summary DB の状態指標カラムを
# 起動時 bootstrap 対象へ登録する。
#
# 目的:
#   - MAクロス状態 / VWAP状態 / VWAP行き来対策列を
#     runtime patch の ALTER TABLE だけに頼らず、
#     システム立ち上げ時に summary DB へ一括作成する
# ============================================================

from __future__ import annotations

import logging

from .session import (
    push_engine,
    summary_engine,
    position_engine,
    ranking_engine,
    tosama_engine,
    Session_push,
    Session_summary,
    Session_position,
    Session_ranking,
    Session_tosama,
    init_engines,
    get_summary_engine,
)

logger = logging.getLogger(__name__)


def _register_summary_state_schema_columns() -> None:
    """
    database.session.SUMMARY_BOOTSTRAP_COLUMNS に、
    状態指標カラムを追加する。

    database.session の _bootstrap_summary_schema() は、
    init_engines() 時に SUMMARY_BOOTSTRAP_COLUMNS を見て
    stock_summary_1min / 3min / 5min の不足列を追加する。
    """
    try:
        import database.session as session_mod
        from database.summary_state_schema import SUMMARY_STATE_BOOTSTRAP_COLUMNS

        current = getattr(session_mod, "SUMMARY_BOOTSTRAP_COLUMNS", None)
        if not isinstance(current, list):
            logger.warning("[DATABASE INIT] SUMMARY_BOOTSTRAP_COLUMNS unavailable")
            return

        existing = {str(col) for col, _typ in current}
        added = []
        for col, typ in SUMMARY_STATE_BOOTSTRAP_COLUMNS:
            if col in existing:
                continue
            current.append((col, typ))
            existing.add(col)
            added.append(col)

        if added:
            logger.warning(
                "[DATABASE INIT] registered summary state schema columns count=%s columns=%s",
                len(added),
                added,
            )
        else:
            logger.info("[DATABASE INIT] summary state schema columns already registered")

    except Exception:
        logger.exception("[DATABASE INIT] register summary state schema columns failed")


_register_summary_state_schema_columns()


__all__ = [
    "push_engine",
    "summary_engine",
    "position_engine",
    "ranking_engine",
    "tosama_engine",
    "Session_push",
    "Session_summary",
    "Session_position",
    "Session_ranking",
    "Session_tosama",
    "init_engines",
    "get_summary_engine",
]
