# ============================================================
# File   : trading/push/subscription_manager/rotation.py
# Version: V1.0-PUSH-SUBSCRIPTION-ROTATION-STRICT-50
# ------------------------------------------------------------
# Purpose:
#   - ranking_selector が作った最大100銘柄候補を、
#     kabu Station PUSH登録上限に合わせて50件ずつに分割する。
#   - 保有中/発注中/直近エントリーなどの priority 銘柄を
#     A/B両方に優先挿入する。
#   - どのreasonでも最終的に50件超を返さない。
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence, Tuple

from .guards import is_on_open_reason
from .symbols import dedupe_keep_order

logger = logging.getLogger(__name__)

REGISTER_CHUNK_SIZE = 50


def normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""

        s = str(v).strip()
        if not s:
            return ""

        if "." in s and s.upper().endswith(".T"):
            s = s.rsplit(".", 1)[0]

        if s.endswith(".0"):
            s2 = s[:-2]
            if s2.isdigit():
                return s2

        return s
    except Exception:
        return ""


def normalize_symbols(values: Any) -> list[str]:
    out: list[str] = []

    try:
        if values is None:
            return []

        if isinstance(values, dict):
            values = list(values.keys())

        if isinstance(values, str):
            values = [values]

        for v in list(values or []):
            s = normalize_symbol(v)
            if s:
                out.append(s)

        return dedupe_keep_order(out)

    except Exception:
        return dedupe_keep_order(out)


def is_rotation_a_reason(reason: str) -> bool:
    s = str(reason or "").strip().lower()
    return s in (
        "rotation_a",
        "rotation-a",
        "rotation a",
        "rotate_a",
        "rotate-a",
        "a",
    )


def is_rotation_b_reason(reason: str) -> bool:
    s = str(reason or "").strip().lower()
    return s in (
        "rotation_b",
        "rotation-b",
        "rotation b",
        "rotate_b",
        "rotate-b",
        "b",
    )


def split_rotation_targets(
    symbols: Sequence[Any],
    priority_symbols: Optional[Sequence[Any]] = None,
    *,
    register_chunk_size: int = REGISTER_CHUNK_SIZE,
) -> Tuple[list[str], list[str]]:
    """
    最大100銘柄候補を rotation_A / rotation_B に分割する。

    重要:
      - A/B どちらも最大50件
      - priority_symbols はA/B両方へ優先挿入
      - priorityが50件超の場合はpriority先頭50件のみ
    """
    chunk = int(register_chunk_size or REGISTER_CHUNK_SIZE)
    if chunk <= 0:
        chunk = REGISTER_CHUNK_SIZE

    items = dedupe_keep_order(normalize_symbols(symbols))
    priority = dedupe_keep_order(normalize_symbols(priority_symbols))

    priority_limited = priority[:chunk]
    priority_set = set(priority_limited)

    non_priority = [s for s in items if s not in priority_set]

    room = max(0, chunk - len(priority_limited))

    rotation_a = dedupe_keep_order(priority_limited + non_priority[:room])
    rotation_b = dedupe_keep_order(priority_limited + non_priority[room:room + room])

    rotation_a = rotation_a[:chunk]
    rotation_b = rotation_b[:chunk]

    logger.info(
        "[SUB MANAGER ROTATION] split total=%d priority=%d A=%d B=%d chunk=%d",
        len(items),
        len(priority_limited),
        len(rotation_a),
        len(rotation_b),
        chunk,
    )

    return rotation_a, rotation_b


def select_target_by_reason(
    symbols: Sequence[Any],
    reason: str,
    priority_symbols: Optional[Sequence[Any]] = None,
    *,
    register_chunk_size: int = REGISTER_CHUNK_SIZE,
) -> list[str]:
    """
    reason に応じて A/B のどちらかを返す。

    V1.0の保証:
      - manual / background_loop / force_refresh / on_open でも
        100銘柄をそのまま返さない
      - 最終返却は常に50件以内
    """
    items = dedupe_keep_order(normalize_symbols(symbols))
    a, b = split_rotation_targets(
        items,
        priority_symbols=priority_symbols,
        register_chunk_size=register_chunk_size,
    )

    r = str(reason or "").strip()

    if is_rotation_b_reason(r):
        selected = b
        selected_name = "B"
    else:
        # rotation_A / on_open / manual / background_loop / force_refresh はA側
        selected = a
        if is_rotation_a_reason(r):
            selected_name = "A"
        elif is_on_open_reason(r):
            selected_name = "ON_OPEN->A"
        else:
            selected_name = "DEFAULT->A"

    selected = selected[: int(register_chunk_size or REGISTER_CHUNK_SIZE)]

    logger.info(
        "[SUB MANAGER ROTATION] reason=%s selected=%s size=%d total_candidates=%d",
        reason,
        selected_name,
        len(selected),
        len(items),
    )

    return selected


def enforce_register_limit(
    symbols: Sequence[Any],
    *,
    register_chunk_size: int = REGISTER_CHUNK_SIZE,
    reason: str = "",
) -> list[str]:
    """
    最終防衛。50件超なら必ず切る。
    """
    chunk = int(register_chunk_size or REGISTER_CHUNK_SIZE)
    if chunk <= 0:
        chunk = REGISTER_CHUNK_SIZE

    out = dedupe_keep_order(normalize_symbols(symbols))

    if len(out) > chunk:
        logger.warning(
            "[SUB MANAGER ROTATION] trim target %d -> %d reason=%s",
            len(out),
            chunk,
            reason,
        )
        out = out[:chunk]

    return out


__all__ = [
    "REGISTER_CHUNK_SIZE",
    "normalize_symbol",
    "normalize_symbols",
    "is_rotation_a_reason",
    "is_rotation_b_reason",
    "split_rotation_targets",
    "select_target_by_reason",
    "enforce_register_limit",
]
