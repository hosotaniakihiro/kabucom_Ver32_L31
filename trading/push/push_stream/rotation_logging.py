# ============================================================
# File   : trading/push/push_stream/rotation_logging.py
# Version: PRODUCTION-STABLE-REV2-PUSH-ROTATION-LOGGING-INDEPENDENT
# ------------------------------------------------------------
# PUSH A/Bローテーション用ログAPI。
#
# Responsibilities:
#   - 登録対象50銘柄を 銘柄コード(銘柄名) 形式で1行表示
#   - 候補銘柄の raw/real/filler/invalid 件数を表示
#
# Notes:
#   - 旧 rotation.py へ依存しない独立実装。
# ============================================================

from __future__ import annotations

import logging
from typing import Sequence

from .rotation_settings import DEFAULT_REGISTER_CHUNK_SIZE
from .rotation_symbols import clean_symbol_list

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV2-PUSH-ROTATION-LOGGING-INDEPENDENT"


def log_register_targets_with_names(
    symbols: Sequence[str],
    *,
    label: str,
    reason: str,
) -> None:
    """登録対象50銘柄を 銘柄コード(銘柄名) 形式で1行表示する。"""
    try:
        cleaned, _, _, _ = clean_symbol_list(symbols)
        cleaned = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

        if not cleaned:
            logger.warning(
                "[PUSH ROTATION REGISTER TARGETS LINE] label=%s reason=%s count=0 symbols=",
                label,
                reason,
            )
            return

        try:
            from trading.push.subscription_manager.register_symbol_logger import (
                format_symbols_one_line,
                load_symbol_name_map,
            )

            name_map = load_symbol_name_map()
            line = format_symbols_one_line(cleaned, symbol_name_map=name_map)
            logger.info(
                "[PUSH ROTATION REGISTER TARGETS LINE] label=%s reason=%s count=%d symbols=%s",
                label,
                reason,
                len(cleaned),
                line,
            )
            return

        except Exception:
            logger.debug(
                "[push_stream] register_symbol_logger name format failed -> code only fallback",
                exc_info=True,
            )

        logger.info(
            "[PUSH ROTATION REGISTER TARGETS LINE] label=%s reason=%s count=%d symbols=%s",
            label,
            reason,
            len(cleaned),
            ", ".join(cleaned),
        )

    except Exception:
        logger.exception(
            "[push_stream] failed to log register targets with names label=%s reason=%s",
            label,
            reason,
        )


def log_candidate_result(
    *,
    source: str,
    raw_count: int,
    real: Sequence[str],
    filler_count: int,
    invalid_count: int,
) -> None:
    logger.info(
        "[push_stream] register target candidate source=%s raw=%d real=%d filler=%d invalid=%d head=%s",
        source,
        raw_count,
        len(real),
        filler_count,
        invalid_count,
        list(real[:10]),
    )


__all__ = [
    "VERSION",
    "log_register_targets_with_names",
    "log_candidate_result",
]
