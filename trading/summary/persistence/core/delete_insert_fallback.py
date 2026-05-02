# ============================================================
# File   : trading/summary/persistence/core/delete_insert_fallback.py
# Version: PRODUCTION-STABLE-REV1.0-SUMMARY-DELETE-INSERT-FALLBACK
# ------------------------------------------------------------
# Purpose:
#   UNIQUE/PK 不在時の DELETE + INSERT fallback
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy.engine import Engine

from database.sqlite.normalize import normalize_rows_for_sqlite
from database.sqlite.retry import run_sql_many_with_retry

from .chunk_utils import valid_conflict_key_rows
from .sql_builder import build_summary_delete_sql, build_summary_insert_sql
from .table_filter import filter_rows_to_existing_columns

logger = logging.getLogger(__name__)


def delete_then_insert_chunk(
    *,
    engine: Engine,
    table_name: str,
    chunk: List[dict],
    interval: int,
    chunk_no: int,
    total_chunks: int,
    retry: int,
    sleep_base: float,
) -> None:
    valid_chunk = valid_conflict_key_rows(chunk)

    if not valid_chunk:
        logger.warning(
            "[UPSERT] delete+insert skipped interval=%s table=%s chunk=%s/%s reason=no valid (symbol, datetime)",
            interval,
            table_name,
            chunk_no,
            total_chunks,
        )
        return

    safe_chunk = normalize_rows_for_sqlite(valid_chunk)
    safe_chunk = filter_rows_to_existing_columns(engine, table_name, safe_chunk)

    if not safe_chunk:
        logger.warning(
            "[UPSERT] delete+insert skipped interval=%s table=%s chunk=%s/%s reason=no rows after table-column filter",
            interval,
            table_name,
            chunk_no,
            total_chunks,
        )
        return

    safe_delete_params: List[Dict[str, Any]] = []

    for row in safe_chunk:
        symbol = row.get("symbol")
        dtv = row.get("datetime")
        if symbol in (None, "") or dtv is None:
            continue
        safe_delete_params.append({"symbol": symbol, "datetime": dtv})

    if not safe_delete_params:
        logger.warning(
            "[UPSERT] delete+insert skipped interval=%s table=%s chunk=%s/%s reason=no normalized delete keys",
            interval,
            table_name,
            chunk_no,
            total_chunks,
        )
        return

    delete_sql = build_summary_delete_sql(table_name)
    insert_sql = build_summary_insert_sql(table_name, safe_chunk)

    run_sql_many_with_retry(
        engine=engine,
        sql=delete_sql,
        params=safe_delete_params,
        log_prefix="[UPSERT] delete fallback",
        interval=interval,
        table_name=table_name,
        chunk_no=chunk_no,
        total_chunks=total_chunks,
        retry=retry,
        sleep_base=sleep_base,
    )

    run_sql_many_with_retry(
        engine=engine,
        sql=insert_sql,
        params=safe_chunk,
        log_prefix="[UPSERT] insert fallback",
        interval=interval,
        table_name=table_name,
        chunk_no=chunk_no,
        total_chunks=total_chunks,
        retry=retry,
        sleep_base=sleep_base,
    )

    logger.warning(
        "[UPSERT] delete+insert fallback ok interval=%s table=%s chunk=%s/%s rows=%s",
        interval,
        table_name,
        chunk_no,
        total_chunks,
        len(safe_chunk),
    )
