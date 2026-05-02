# ============================================================
# File   : trading/summary/persistence/core/chunk_utils.py
# Version: PRODUCTION-STABLE-REV1.0-SUMMARY-CHUNK-UTILS
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Sequence, Tuple

logger = logging.getLogger(__name__)


def chunked(rows: Sequence[dict], size: int) -> Iterable[Tuple[int, int, List[dict]]]:
    total = len(rows)
    if total <= 0:
        return

    safe_size = max(1, int(size or 1))
    total_chunks = (total + safe_size - 1) // safe_size

    chunk_no = 0
    for start in range(0, total, safe_size):
        chunk_no += 1
        end = min(start + safe_size, total)
        yield chunk_no, total_chunks, list(rows[start:end])


def normalize_rows(rows: Any) -> List[dict]:
    if rows is None:
        return []

    if isinstance(rows, list):
        out: List[dict] = []
        for r in rows:
            if isinstance(r, dict) and r:
                out.append(dict(r))
        return out

    try:
        if hasattr(rows, "to_dict"):
            data = rows.to_dict(orient="records")
            return [dict(r) for r in data if isinstance(r, dict) and r]
    except Exception:
        logger.exception("[UPSERT] failed to normalize dataframe-like rows")

    return []


def valid_conflict_key_rows(rows: Sequence[dict]) -> List[dict]:
    out: List[dict] = []

    for r in rows:
        if (
            "symbol" in r
            and "datetime" in r
            and r.get("symbol") not in (None, "")
            and r.get("datetime") is not None
        ):
            out.append(r)

    return out
