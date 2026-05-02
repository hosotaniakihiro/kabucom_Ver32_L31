# ============================================================
# File   : database/migrate_bandit.py
# Version: V32-FINAL-BANDIT-MIGRATE-PATHS-UNIFIED
# ------------------------------------------------------------
# ✔ config.paths 連携
# ✔ BaseBandit 再利用（モデル重複排除）
# ✔ WAL / busy_timeout
# ✔ ADD ONLY思想（create_allのみ）
# ✔ 既存DB絶対破壊禁止
# ✔ UNC完全対応
# ✔ 例外安全
# ============================================================

from __future__ import annotations

import logging
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from config.paths import get_path, ensure_dirs
from database.bandit_models import BaseBandit

logger = logging.getLogger(__name__)


# ============================================================
# Engine
# ============================================================

def create_sqlite_engine() -> Engine:
    """
    WAL + busy_timeout 対応 SQLite Engine
    """

    db_path = get_path("bandit_db")

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.close()

    return engine


# ============================================================
# Migration
# ============================================================

def ensure_bandit_db():
    """
    ADD ONLY思想：
        - create_allのみ
        - ALTERやDROPは絶対しない
    """

    try:
        # ディレクトリ生成保証
        ensure_dirs()

        db_path = get_path("bandit_db")

        logger.info(
            "[MIGRATE] Ensuring Bandit DB: %s",
            db_path
        )

        engine = create_sqlite_engine()

        # ADD ONLY
        BaseBandit.metadata.create_all(engine)

        logger.info("[MIGRATE] Bandit DB ready.")

    except Exception:
        logger.exception("[MIGRATE] Bandit DB migration failed")