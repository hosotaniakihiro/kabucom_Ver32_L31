# ============================================================
# File   : database/reset_summary_tables.py
# Version: Ver1.0-PRODUCTION-SUMMARY-TABLE-RESET-FINAL
# ------------------------------------------------------------
# ✔ stock_summary_3min / 5min を安全再作成
# ✔ 旧UNIQUE制約残骸を完全除去
# ✔ database.models 定義に完全追従
# ✔ 1min は保持
# ✔ SQLite / SQLAlchemy 安全実行
# ✔ WAL考慮
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging

from sqlalchemy import text

from database.session import get_summary_engine
from database.models import StockSummary3Min, StockSummary5Min

logger = logging.getLogger(__name__)


def _enable_wal(conn) -> None:
    try:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
        conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
        conn.exec_driver_sql("PRAGMA busy_timeout=30000;")
    except Exception:
        logger.exception("[SUMMARY RESET] failed to set WAL pragmas")


def reset_summary_tables(drop_1min: bool = False) -> None:
    """
    summary DB の 3min / 5min テーブルを一旦削除し、
    database.models の ORM 定義どおりに再作成する。

    Parameters
    ----------
    drop_1min : bool
        True の場合のみ 1min も削除再作成する。
        通常は False のままを推奨。
    """
    engine = get_summary_engine()
    if engine is None:
        raise RuntimeError("[SUMMARY RESET] get_summary_engine() returned None")

    logger.warning("[SUMMARY RESET] start")

    with engine.begin() as conn:
        _enable_wal(conn)

        # 念のため古い index 残骸も落とす
        stale_indexes = [
            "ix_stock_summary_3min_symbol",
            "ix_stock_summary_3min_datetime",
            "ix_stock_summary_3min_date",
            "ix_stock_summary_3min_time_range",
            "ix_stock_summary_3min_source",
            "ix_stock_summary_5min_symbol",
            "ix_stock_summary_5min_datetime",
            "ix_stock_summary_5min_date",
            "ix_stock_summary_5min_time_range",
            "ix_stock_summary_5min_source",
        ]

        for idx in stale_indexes:
            try:
                conn.execute(text(f'DROP INDEX IF EXISTS "{idx}"'))
            except Exception:
                logger.exception("[SUMMARY RESET] failed to drop stale index: %s", idx)

        # テーブル削除
        if drop_1min:
            try:
                conn.execute(text('DROP TABLE IF EXISTS "stock_summary_1min"'))
                logger.warning("[SUMMARY RESET] dropped stock_summary_1min")
            except Exception:
                logger.exception("[SUMMARY RESET] failed to drop stock_summary_1min")
                raise

        try:
            conn.execute(text('DROP TABLE IF EXISTS "stock_summary_3min"'))
            logger.warning("[SUMMARY RESET] dropped stock_summary_3min")
        except Exception:
            logger.exception("[SUMMARY RESET] failed to drop stock_summary_3min")
            raise

        try:
            conn.execute(text('DROP TABLE IF EXISTS "stock_summary_5min"'))
            logger.warning("[SUMMARY RESET] dropped stock_summary_5min")
        except Exception:
            logger.exception("[SUMMARY RESET] failed to drop stock_summary_5min")
            raise

    # ORM定義から再作成
    try:
        if drop_1min:
            from database.models import StockSummary1Min
            StockSummary1Min.__table__.create(bind=engine, checkfirst=True)
            logger.info("[SUMMARY RESET] created stock_summary_1min")

        StockSummary3Min.__table__.create(bind=engine, checkfirst=True)
        logger.info("[SUMMARY RESET] created stock_summary_3min")

        StockSummary5Min.__table__.create(bind=engine, checkfirst=True)
        logger.info("[SUMMARY RESET] created stock_summary_5min")

    except Exception:
        logger.exception("[SUMMARY RESET] create failed")
        raise

    logger.warning("[SUMMARY RESET] done")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    reset_summary_tables(drop_1min=False)