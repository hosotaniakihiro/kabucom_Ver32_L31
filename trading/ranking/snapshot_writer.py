# ============================================================
# File   : trading/ranking/snapshot_writer.py
# Version: COMPAT-REV2.0-DELEGATE-TO-DATABASE
# ------------------------------------------------------------
# 【概要】
#   旧 import 互換用。
#   実体は database.upsert.ranking_snapshot_upsert へ移動。
# ============================================================

from __future__ import annotations

from database.paths.ranking_paths import resolve_ranking_db_path
from database.schema.ranking_snapshot_schema import (
    SNAPSHOT_TABLE,
    SNAPSHOT_UNIQUE_INDEX,
    ensure_ranking_snapshot_table,
    ensure_ranking_snapshot_unique_index,
    patch_ranking_snapshot_schema,
)
from database.upsert.ranking_snapshot_upsert import (
    normalize_datetime_text,
    normalize_snapshot_row,
    save_ranking_snapshot_rows,
)

# 旧名互換
resolve_ranking_db_path = resolve_ranking_db_path

__all__ = [
    "SNAPSHOT_TABLE",
    "SNAPSHOT_UNIQUE_INDEX",
    "resolve_ranking_db_path",
    "ensure_ranking_snapshot_table",
    "patch_ranking_snapshot_schema",
    "ensure_ranking_snapshot_unique_index",
    "normalize_datetime_text",
    "normalize_snapshot_row",
    "save_ranking_snapshot_rows",
]