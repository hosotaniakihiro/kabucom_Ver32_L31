# ============================================================
# File   : ats/ats_register.py
# Ver48-PRODUCTION-BATCH50-UNREGISTER-ALL-RESTORE-MODULARIZED
#      -RANKING-ONLY-PUSH-TARGETS
#      -RANKINFO-FALLBACK-FROM-ATS-RANKING
# ------------------------------------------------------------
# ✔ 旧 ats_register.py の公開API維持
# ✔ 内部実装を module 分割
# ✔ ats_register_loop / show_should_register_symbols 互換維持
# ============================================================

from __future__ import annotations

import logging

from global_state import global_data
from ats.ats_rotation_manager import ATSRotationManager
from ats.ats_filters import apply_all_filters

from .ats_register_state import ATS_BATCH_SIZE, sanitize_symbols, split_batches_50
from .ats_register_loop import ats_register_loop, build_registerable_symbol_pool
from .ats_register_payload import unregister_symbols, unregister_all_vendor, register_symbols_batch
from .ats_register_logging import (
    show_current_ats_registered_symbols,
    _print_current_ats_registered_symbols,
)

logger = logging.getLogger(__name__)


def ats_register_symbol_once(symbol):
    return True, "ignored"


def show_should_register_symbols(limit: int = 100) -> None:
    try:
        rotator = ATSRotationManager(batch_size=100, shift=50)

        registerable_targets = build_registerable_symbol_pool(
            rotator=rotator,
            target_size=ATS_BATCH_SIZE,
            max_attempts=10,
        )

        registerable_targets = sanitize_symbols(registerable_targets)
        batch_a, batch_b = split_batches_50(registerable_targets)

        logger.info(
            "[ATS SHOULD REGISTER] total=%d batchA=%d batchB=%d",
            len(registerable_targets),
            len(batch_a),
            len(batch_b),
        )

        _print_current_ats_registered_symbols(
            registerable_targets[:limit],
            phase="SHOULD-REGISTER"
        )

    except Exception:
        logger.exception("show_should_register_symbols failed")


__all__ = [
    "ats_register_loop",
    "ats_register_symbol_once",
    "show_should_register_symbols",
    "show_current_ats_registered_symbols",
    "unregister_symbols",
    "unregister_all_vendor",
    "register_symbols_batch",
]