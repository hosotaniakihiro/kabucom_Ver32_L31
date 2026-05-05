# ============================================================
# File   : trading/push/push_stream/rotation.py
# Version: PRODUCTION-STABLE-REV3-PUSH-ROTATION-COMPAT-WRAPPER
# ------------------------------------------------------------
# 旧 import path 互換用ラッパー。
#
# 実体は以下へ分割済み:
#   - rotation_core.py
#   - rotation_settings.py
#   - rotation_symbols.py
#   - rotation_register.py
#   - rotation_logging.py
#
# Notes:
#   - runner.py は rotation_core._rotation_worker を直接起動する
#   - このファイルは旧コードから
#       from trading.push.push_stream.rotation import ...
#     された場合の互換性維持用
# ============================================================

from __future__ import annotations

from .rotation_core import (
    VERSION as CORE_VERSION,
    enable_rotation,
    _rotation_worker,
    _sleep_or_stop,
)

from .rotation_settings import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
    ROTATE_HOLD_SEC,
    UNREGISTER_TO_REGISTER_WAIT_SEC,
    WS_WAIT_LOG_INTERVAL_SEC,
    REGISTER_TIMEOUT_SEC,
)

from .rotation_symbols import (
    is_filler_symbol as _is_filler_symbol,
    is_real_symbol as _is_real_symbol,
    normalize_real_symbol as _normalize_real_symbol,
    dedupe_keep_order as _dedupe_keep_order,
    clean_symbol_list as _clean_symbol_list,
    resolve_monitor_symbols as _resolve_monitor_symbols,
    apply_register_liquidity_guard as _apply_register_liquidity_guard,
    resolve_register_targets as _resolve_register_targets,
    safe_call_provider as _safe_call_provider,
)

from .rotation_register import (
    refresh_result_to_ok as _refresh_result_to_ok,
    register_symbols,
    run_one_batch as _run_one_batch,
    run_one_batch_with_timeout as _run_one_batch_with_timeout,
)

from .rotation_logging import (
    log_register_targets_with_names as _log_register_targets_with_names,
    log_candidate_result as _log_candidate_result,
)

VERSION = "PRODUCTION-STABLE-REV3-PUSH-ROTATION-COMPAT-WRAPPER"

__all__ = [
    "VERSION",
    "CORE_VERSION",
    "DEFAULT_REGISTER_CHUNK_SIZE",
    "DEFAULT_REGISTER_MAX_SYMBOLS",
    "ROTATE_HOLD_SEC",
    "UNREGISTER_TO_REGISTER_WAIT_SEC",
    "WS_WAIT_LOG_INTERVAL_SEC",
    "REGISTER_TIMEOUT_SEC",
    "enable_rotation",
    "register_symbols",
    "_rotation_worker",
    "_sleep_or_stop",
    "_is_filler_symbol",
    "_is_real_symbol",
    "_normalize_real_symbol",
    "_dedupe_keep_order",
    "_clean_symbol_list",
    "_resolve_monitor_symbols",
    "_apply_register_liquidity_guard",
    "_resolve_register_targets",
    "_safe_call_provider",
    "_refresh_result_to_ok",
    "_run_one_batch",
    "_run_one_batch_with_timeout",
    "_log_register_targets_with_names",
    "_log_candidate_result",
]
