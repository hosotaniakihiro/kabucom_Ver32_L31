# ============================================================
# File   : trading/summary/persistence/core/table_filter.py
# Version: PRODUCTION-STABLE-REV1.0-SUMMARY-TABLE-FILTER
# ------------------------------------------------------------
# Purpose:
#   実テーブルに存在する列だけへ rows を絞る。
# ============================================================

from __future__ import annotations

import logging
from typing import List, Sequence

from sqlalchemy.engine import Engine

from database.sqlite.inspector import read_table_columns

from .chunk_utils import valid_conflict_key_rows

logger = logging.getLogger(__name__)


def filter_rows_to_existing_columns(
    engine: Engine,
    table_name: str,
    rows: Sequence[dict],
) -> List[dict]:
    if not rows:
        return []

    table_cols = set(read_table_columns(engine, table_name))
    if not table_cols:
        logger.warning("[UPSERT] no table columns found -> skip filtering table=%s", table_name)
        return [dict(r) for r in rows]

    out: List[dict] = []
    dropped_cols_logged = set()

    for row in rows:
        nr = {}
        for k, v in row.items():
            if k in table_cols:
                nr[k] = v
            else:
                if k not in dropped_cols_logged:
                    dropped_cols_logged.add(k)
                    logger.warning("[UPSERT] dropping unknown column table=%s column=%s", table_name, k)

        if nr:
            out.append(nr)

    filtered = valid_conflict_key_rows(out)
    dropped_rows = len(out) - len(filtered)

    if dropped_rows > 0:
        logger.warning(
            "[UPSERT] rows dropped after column filter table=%s dropped_rows=%s",
            table_name,
            dropped_rows,
        )

    return filtered
