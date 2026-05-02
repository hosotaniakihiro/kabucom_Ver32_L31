# ============================================================
# File   : trading/entry/pipeline/pending_bridge.py
# Function:
#   - pending_entries 操作
#   - pending_manager 優先 / global_data.pending_entries fallback
#   - pending 統計 / 詳細ログ
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-PENDING-BRIDGE
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, List

from .imports import (
    global_data,
    get_bucket,
    replace_bucket,
)

from .utils import (
    safe_symbol,
    safe_str,
    safe_int,
    safe_bool,
    normalize_side,
    candidate_score,
)

logger = logging.getLogger(__name__)


def ensure_pending_root() -> Dict[str, List[dict]]:
    try:
        root = getattr(global_data, "pending_entries", None)

        if isinstance(root, dict):
            return root

    except Exception:
        pass

    try:
        global_data.pending_entries = {}
        return global_data.pending_entries
    except Exception:
        return {}


def dedupe_entries_keep_best(entries: List[dict]) -> List[dict]:
    """
    pending entries を重複排除する。

    同一:
      - symbol
      - side
      - interval
      - entry_type
      - source
      - matched_sources

    score が高いものを残す。
    """

    if not entries:
        return []

    best_map: Dict[tuple, dict] = {}

    for item in entries:
        if not isinstance(item, dict):
            continue

        symbol = safe_symbol(item.get("symbol"))
        side = normalize_side(item.get("side") or item.get("entry_decision"))
        interval = safe_int(item.get("interval"), 0)
        entry_type = safe_str(item.get("entry_type"))
        source = safe_str(item.get("source"))

        matched_sources = item.get("matched_sources") or []

        if isinstance(matched_sources, list):
            matched_key = ",".join(sorted(safe_str(x) for x in matched_sources if safe_str(x)))
        else:
            matched_key = safe_str(matched_sources)

        if not symbol or side not in ("BUY", "SELL"):
            continue

        key = (
            symbol,
            side,
            interval,
            entry_type,
            source,
            matched_key,
        )

        score = candidate_score(item)

        prev = best_map.get(key)

        if prev is None:
            best_map[key] = item
            continue

        prev_score = candidate_score(prev)

        if score >= prev_score:
            best_map[key] = item

    out = list(best_map.values())

    out.sort(
        key=lambda x: (
            safe_symbol(x.get("symbol")),
            normalize_side(x.get("side") or x.get("entry_decision")) or "",
            -candidate_score(x),
        )
    )

    return out


def set_bucket_compatible(symbol: str, bucket: List[dict]) -> bool:
    symbol = safe_symbol(symbol)

    if not symbol:
        return False

    bucket = [x for x in bucket if isinstance(x, dict)]
    bucket = dedupe_entries_keep_best(bucket)

    if replace_bucket is not None:
        try:
            replace_bucket(symbol, bucket)
            return True
        except Exception:
            logger.exception("[PENDING] replace_bucket failed symbol=%s", symbol)

    try:
        root = ensure_pending_root()
        root[symbol] = bucket
        global_data.pending_entries = root
        return True
    except Exception:
        logger.exception("[PENDING] direct bucket fallback failed symbol=%s", symbol)
        return False


def get_bucket_compatible(symbol: str) -> List[dict]:
    symbol = safe_symbol(symbol)

    if not symbol:
        return []

    if get_bucket is not None:
        try:
            bucket = get_bucket(symbol)

            if isinstance(bucket, list):
                return [x for x in bucket if isinstance(x, dict)]

        except Exception:
            logger.exception("[PENDING] get_bucket failed symbol=%s", symbol)

    try:
        root = getattr(global_data, "pending_entries", None)

        if isinstance(root, dict):
            bucket = root.get(symbol, [])

            if isinstance(bucket, list):
                return [x for x in bucket if isinstance(x, dict)]

    except Exception:
        logger.exception("[PENDING] direct get bucket failed symbol=%s", symbol)

    return []


def append_entries_to_pending(entries: List[dict], *, log_prefix: str) -> int:
    """
    entries を pending_entries に追加する共通処理。
    """

    try:
        if not entries:
            logger.info("%s no entries", log_prefix)
            return 0

        grouped: Dict[str, List[dict]] = {}

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            symbol = safe_symbol(entry.get("symbol"))

            if not symbol:
                continue

            grouped.setdefault(symbol, []).append(entry)

        created = 0
        touched_symbols = 0

        for symbol, symbol_entries in grouped.items():
            existing = get_bucket_compatible(symbol)
            merged = list(existing) + list(symbol_entries)
            merged = dedupe_entries_keep_best(merged)

            if set_bucket_compatible(symbol, merged):
                touched_symbols += 1
                created += len(symbol_entries)

        pending_root = getattr(global_data, "pending_entries", None)
        pending_symbols = len(pending_root) if isinstance(pending_root, dict) else 0

        logger.info(
            "%s created_entries=%d touched_symbols=%d pending_symbols=%d",
            log_prefix,
            created,
            touched_symbols,
            pending_symbols,
        )

        return created

    except Exception:
        logger.exception("%s append failed", log_prefix)
        return 0


def count_pending_stats() -> tuple[int, int, int]:
    try:
        root = getattr(global_data, "pending_entries", None)

        if not isinstance(root, dict) or not root:
            return 0, 0, 0

        symbols = 0
        entries = 0
        ai_allow_true = 0

        for symbol, bucket in root.items():
            if not symbol or not isinstance(bucket, list):
                continue

            symbols += 1

            for item in bucket:
                if not isinstance(item, dict):
                    continue

                entries += 1

                if safe_bool(item.get("ai_allow"), False):
                    ai_allow_true += 1

        return symbols, entries, ai_allow_true

    except Exception:
        logger.exception("[PENDING STATS] failed")
        return 0, 0, 0


def log_pending_detail(limit: int = 10) -> None:
    """
    pending_entries の内容確認用ログ。
    """

    try:
        root = getattr(global_data, "pending_entries", None)

        if not isinstance(root, dict) or not root:
            logger.info("[PENDING DETAIL] empty")
            return

        rows: List[dict] = []

        for symbol, bucket in root.items():
            if not isinstance(bucket, list):
                continue

            for item in bucket:
                if isinstance(item, dict):
                    rows.append(item)

        rows.sort(key=lambda x: candidate_score(x), reverse=True)

        for i, item in enumerate(rows[: int(limit)], start=1):
            logger.info(
                "[PENDING DETAIL] #%02d symbol=%s name=%s side=%s type=%s source=%s "
                "matched=%s score=%.4f ai_allow=%s reason=%s",
                i,
                item.get("symbol"),
                item.get("symbolname"),
                item.get("side"),
                item.get("entry_type"),
                item.get("source"),
                item.get("matched_sources"),
                candidate_score(item),
                item.get("ai_allow"),
                item.get("reason"),
            )

    except Exception:
        logger.exception("[PENDING DETAIL] failed")