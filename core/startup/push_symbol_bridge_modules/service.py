# ============================================================
# File   : core/startup/push_symbol_bridge_modules/service.py
# Version: PRODUCTION-STABLE-REV3.0
# ------------------------------------------------------------
# Purpose:
#   push_symbol_bridge のメインサービス。
#
# Flow:
#   resolve_real_push_symbols() で100銘柄候補を解決
#     ↓
#   split_register_rotation() で A/B 50件へ分割
#     ↓
#   select_register_symbols() で今回注入する50件を選択
#     ↓
#   global_data / push_stream.runtime へ
#     candidate100 と register50 を分けて注入
#     ↓
#   登録対象50銘柄を1行ログ表示
# ============================================================

from __future__ import annotations

import logging
from typing import List

from .constants import (
    DEFAULT_MAX_SYMBOLS,
    DEFAULT_REGISTER_LIMIT,
    VERSION,
)
from .injectors import (
    set_global_data_symbols,
    set_push_stream_runtime_symbols,
)
from .normalize import clean_symbols
from .providers import resolve_real_push_symbols
from .register_name_logger import log_register_symbols_with_names
from .rotation import select_register_symbols, split_register_rotation

logger = logging.getLogger(__name__)


def install_real_push_symbols(
    *,
    limit: int = DEFAULT_MAX_SYMBOLS,
    register_limit: int = DEFAULT_REGISTER_LIMIT,
    rotation: str = "A",
    strict: bool = False,
) -> List[str]:
    """
    実銘柄を解決し、global_data / push_stream.runtime へ注入する。

    Returns:
      100件候補リスト

    注意:
      - 戻り値は候補100件
      - 登録系属性には register_limit 件だけ注入する
    """
    candidate_symbols = resolve_real_push_symbols(limit=limit)
    candidate_symbols = clean_symbols(candidate_symbols, limit=limit)

    if not candidate_symbols:
        msg = "[PUSH SYMBOL BRIDGE] install failed: no real symbols"
        if strict:
            raise RuntimeError(msg)
        logger.error(msg)
        return []

    rotation_a, rotation_b = split_register_rotation(
        candidate_symbols,
        register_limit=register_limit,
    )

    register_symbols = select_register_symbols(
        candidate_symbols,
        register_limit=register_limit,
        rotation=rotation,
    )

    if len(register_symbols) > int(register_limit or DEFAULT_REGISTER_LIMIT):
        logger.warning(
            "[PUSH SYMBOL BRIDGE] register symbols exceeded limit. trim %d -> %d",
            len(register_symbols),
            int(register_limit or DEFAULT_REGISTER_LIMIT),
        )
        register_symbols = register_symbols[: int(register_limit or DEFAULT_REGISTER_LIMIT)]

    set_global_data_symbols(
        candidate_symbols,
        register_symbols,
        rotation_a_symbols=rotation_a,
        rotation_b_symbols=rotation_b,
    )

    set_push_stream_runtime_symbols(
        candidate_symbols,
        register_symbols,
        rotation_a_symbols=rotation_a,
        rotation_b_symbols=rotation_b,
    )

    log_register_symbols_with_names(
        register_symbols,
        reason=f"startup_bridge_rotation_{str(rotation or 'A').upper()}",
    )

    logger.info(
        "[PUSH SYMBOL BRIDGE] install complete version=%s candidate100=%d "
        "register50=%d rotation_A=%d rotation_B=%d candidate_head=%s register_head=%s",
        VERSION,
        len(candidate_symbols),
        len(register_symbols),
        len(rotation_a),
        len(rotation_b),
        candidate_symbols[:10],
        register_symbols[:10],
    )

    return candidate_symbols


__all__ = [
    "VERSION",
    "install_real_push_symbols",
]
