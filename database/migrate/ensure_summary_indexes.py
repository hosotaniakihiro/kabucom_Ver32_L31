# ============================================================
# File   : database/migrate/ensure_summary_indexes.py
# Ver    : PRODUCTION-STABLE-REV1.0-SUMMARY-SNAPSHOT-INDEX-ENSURE
# ------------------------------------------------------------
# 【概要】
#   summary DB の snapshot 読み込み高速化用 index を保証する
#
# 【主な機能】
#   - stock_summary_1min / 3min / 5min に
#     (symbol, datetime) 複合 index を作成
#   - snapshot preload 用 SQL の高速化
#   - SAFE MIGRATION / DB bootstrap から安全に呼び出し可能
#
# 【設計方針】
#   - CREATE INDEX IF NOT EXISTS を使い冪等に動く
#   - index 作成失敗時も全体停止しない
#   - summary_engine が使えない場合は安全に skip
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import text

logger = logging.getLogger(__name__)


_INDEX_SQLS = [
    (
        "idx_stock_summary_1min_symbol_datetime",
        """
        CREATE INDEX IF NOT EXISTS idx_stock_summary_1min_symbol_datetime
        ON stock_summary_1min(symbol, datetime)
        """,
    ),
    (
        "idx_stock_summary_3min_symbol_datetime",
        """
        CREATE INDEX IF NOT EXISTS idx_stock_summary_3min_symbol_datetime
        ON stock_summary_3min(symbol, datetime)
        """,
    ),
    (
        "idx_stock_summary_5min_symbol_datetime",
        """
        CREATE INDEX IF NOT EXISTS idx_stock_summary_5min_symbol_datetime
        ON stock_summary_5min(symbol, datetime)
        """,
    ),
]


def _iter_index_sqls() -> Iterable[tuple[str, str]]:
    for name, sql in _INDEX_SQLS:
        yield name, sql.strip()


def ensure_summary_snapshot_indexes(summary_engine) -> None:
    """
    load_latest_summary_snapshot() を高速化するための index を作成する。

    Parameters
    ----------
    summary_engine:
        SQLAlchemy engine for summary DB.
    """
    if summary_engine is None:
        logger.warning("[summary_index] summary_engine is None -> skipped")
        return

    try:
        with summary_engine.begin() as conn:
            for idx_name, sql in _iter_index_sqls():
                try:
                    conn.execute(text(sql))
                    logger.info("[summary_index] ensured %s", idx_name)
                except Exception:
                    logger.exception("[summary_index] failed ensure %s", idx_name)

        logger.info("[summary_index] snapshot indexes ensure complete")

    except Exception:
        logger.exception("[summary_index] ensure failed at engine level")


__all__ = [
    "ensure_summary_snapshot_indexes",
]