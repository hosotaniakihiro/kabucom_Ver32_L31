# ============================================================
# File   : database/crud_ranking.py
# Version: COMPAT-REV1.1-DELEGATE-TO-DATABASE-CRUD-RANKING
# ------------------------------------------------------------
# 【概要】
#   旧 import 互換用ファイル。
#
#   旧:
#       from database.crud_ranking import save_ranking_rows
#
#   新本体:
#       from database.crud.crud_ranking import save_ranking_rows
#
# 【重要】
#   ここに古い保存ロジックを残すと、
#   ranking_raw_1min / ranking_snapshot_1min に保存されない経路が残る。
#
#   そのため、このファイルは本体処理を持たず、
#   database/crud/crud_ranking.py へ委譲する。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# main CRUD delegates
# ============================================================

try:
    from database.crud.crud_ranking import (
        save_ranking_rows,
        get_latest_ranking,
        get_top_symbols,
    )
except Exception:
    logger.exception("[crud_ranking compat] failed to import database.crud.crud_ranking")

    def save_ranking_rows(*args, **kwargs) -> int:
        logger.error("[crud_ranking compat] save_ranking_rows unavailable")
        return 0

    def get_latest_ranking(*args, **kwargs):
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return []

    def get_top_symbols(*args, **kwargs) -> list[str]:
        return []


# ============================================================
# snapshot delegates
# ============================================================

try:
    from database.crud.crud_ranking_snapshot import (
        insert_ranking_snapshot_1min,
        save_ranking_snapshot_1min,
    )
except Exception:
    logger.exception("[crud_ranking compat] failed to import crud_ranking_snapshot")

    def insert_ranking_snapshot_1min(*args, **kwargs) -> int:
        logger.error("[crud_ranking compat] insert_ranking_snapshot_1min unavailable")
        return 0

    def save_ranking_snapshot_1min(*args, **kwargs) -> int:
        logger.error("[crud_ranking compat] save_ranking_snapshot_1min unavailable")
        return 0


# ============================================================
# raw delegates
# ============================================================

try:
    from database.crud.crud_ranking_raw import (
        insert_ranking_raw_1min,
        ensure_ranking_raw_1min_table,
    )
except Exception:
    logger.exception("[crud_ranking compat] failed to import crud_ranking_raw")

    def insert_ranking_raw_1min(*args, **kwargs) -> int:
        logger.error("[crud_ranking compat] insert_ranking_raw_1min unavailable")
        return 0

    def ensure_ranking_raw_1min_table(*args, **kwargs) -> None:
        logger.error("[crud_ranking compat] ensure_ranking_raw_1min_table unavailable")
        return None


# ============================================================
# MA delegate / fallback
# ============================================================

def build_ranking_ma_1min(*args: Any, **kwargs: Any) -> int:
    """
    旧互換用。

    以前は database/crud_ranking.py 内に実装があったが、
    現在はランキング保存本体とは分離する。

    優先:
      1. database.crud.crud_ranking_ma.build_ranking_ma_1min
      2. trading.ranking.summary 等に同等実装があればそこへ委譲
      3. 見つからなければ 0 を返す
    """
    try:
        from database.crud.crud_ranking_ma import build_ranking_ma_1min as fn
        return int(fn(*args, **kwargs) or 0)
    except Exception:
        logger.debug(
            "[crud_ranking compat] crud_ranking_ma delegate unavailable",
            exc_info=True,
        )

    try:
        from database.crud.crud_ranking import build_ranking_ma_1min as fn
        return int(fn(*args, **kwargs) or 0)
    except Exception:
        logger.debug(
            "[crud_ranking compat] crud_ranking.build_ranking_ma_1min unavailable",
            exc_info=True,
        )

    logger.warning("[crud_ranking compat] build_ranking_ma_1min unavailable")
    return 0


# ============================================================
# optional aliases
# ============================================================

def save_ranking_rows_and_snapshot(*args: Any, **kwargs: Any):
    """
    旧コードが save_ranking_rows_and_snapshot を呼ぶ場合の互換入口。
    現在の save_ranking_rows はカテゴリ / snapshot / raw を同時保存するため、
    そのまま委譲する。
    """
    return save_ranking_rows(*args, **kwargs)


def get_top_symbols_from_ranking(*args: Any, **kwargs: Any):
    """
    旧コード互換。
    """
    return get_top_symbols(*args, **kwargs)


# ============================================================
# exports
# ============================================================

__all__ = [
    "save_ranking_rows",
    "save_ranking_rows_and_snapshot",
    "get_latest_ranking",
    "get_top_symbols",
    "get_top_symbols_from_ranking",
    "insert_ranking_snapshot_1min",
    "save_ranking_snapshot_1min",
    "insert_ranking_raw_1min",
    "ensure_ranking_raw_1min_table",
    "build_ranking_ma_1min",
]