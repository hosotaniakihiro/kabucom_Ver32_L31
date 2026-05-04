# ============================================================
# File   : core/startup/push_symbol_bridge_modules/rotation.py
# Version: PRODUCTION-STABLE-REV3.0
# ------------------------------------------------------------
# Purpose:
#   - 100銘柄候補を rotation A/B の50件に分割
#   - 登録系属性へ注入する50銘柄を選択
# ============================================================

from __future__ import annotations

import logging
from typing import Sequence, Tuple, List

from .constants import DEFAULT_MAX_SYMBOLS, DEFAULT_REGISTER_LIMIT
from .normalize import clean_symbols

logger = logging.getLogger(__name__)


def split_register_rotation(
    symbols: Sequence[str],
    *,
    register_limit: int = DEFAULT_REGISTER_LIMIT,
) -> Tuple[List[str], List[str]]:
    """
    100銘柄候補を rotation A/B に分ける。

    Returns:
      (rotation_a_50, rotation_b_50)
    """
    limit = int(register_limit or DEFAULT_REGISTER_LIMIT)
    if limit <= 0:
        limit = DEFAULT_REGISTER_LIMIT

    cleaned = clean_symbols(symbols, limit=DEFAULT_MAX_SYMBOLS)

    a = cleaned[:limit]
    b = cleaned[limit : limit * 2]

    logger.info(
        "[PUSH SYMBOL BRIDGE] split register rotation total=%d A=%d B=%d limit=%d",
        len(cleaned),
        len(a),
        len(b),
        limit,
    )

    return a, b


def select_register_symbols(
    candidate_symbols: Sequence[str],
    *,
    register_limit: int = DEFAULT_REGISTER_LIMIT,
    rotation: str = "A",
) -> List[str]:
    """
    登録系属性へ注入する50銘柄を返す。
    デフォルトは rotation A。
    """
    a, b = split_register_rotation(
        candidate_symbols,
        register_limit=register_limit,
    )

    r = str(rotation or "A").strip().upper()

    if r in ("B", "ROTATION_B", "1", "SECOND", "NEXT"):
        selected = b
        selected_name = "B"
    else:
        selected = a
        selected_name = "A"

    logger.info(
        "[PUSH SYMBOL BRIDGE] selected register rotation=%s count=%d head=%s",
        selected_name,
        len(selected),
        selected[:10],
    )

    return selected[: int(register_limit or DEFAULT_REGISTER_LIMIT)]


__all__ = [
    "DEFAULT_MAX_SYMBOLS",
    "DEFAULT_REGISTER_LIMIT",
    "split_register_rotation",
    "select_register_symbols",
]
