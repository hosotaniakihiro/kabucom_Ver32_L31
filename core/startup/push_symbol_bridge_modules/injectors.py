# ============================================================
# File   : core/startup/push_symbol_bridge_modules/injectors.py
# Version: PRODUCTION-STABLE-REV3.0
# ------------------------------------------------------------
# Purpose:
#   - global_data に100件候補と50件登録対象を分けて注入
#   - push_stream.runtime に100件候補と50件登録対象を分けて注入
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional, Sequence

from .constants import DEFAULT_MAX_SYMBOLS, DEFAULT_REGISTER_LIMIT
from .import_utils import get_global_data
from .normalize import clean_symbols
from .rotation import split_register_rotation

logger = logging.getLogger(__name__)


def set_attr_safe(obj: Any, attr: str, value: Sequence[str]) -> None:
    try:
        setattr(obj, attr, list(value))
    except Exception:
        logger.debug(
            "[PUSH SYMBOL BRIDGE] setattr global_data.%s failed",
            attr,
            exc_info=True,
        )


def call_setter_safe(obj: Any, name: str, value: Sequence[str]) -> None:
    try:
        fn = getattr(obj, name, None)
    except Exception:
        fn = None

    if not callable(fn):
        return

    try:
        fn(list(value))
    except Exception:
        logger.debug(
            "[PUSH SYMBOL BRIDGE] global_data.%s failed",
            name,
            exc_info=True,
        )


def set_global_data_symbols(
    candidate_symbols: Sequence[str],
    register_symbols: Sequence[str],
    *,
    rotation_a_symbols: Optional[Sequence[str]] = None,
    rotation_b_symbols: Optional[Sequence[str]] = None,
) -> None:
    gd = get_global_data()
    if gd is None:
        logger.warning("[PUSH SYMBOL BRIDGE] global_data not found; skip global injection")
        return

    candidates100 = clean_symbols(candidate_symbols, limit=DEFAULT_MAX_SYMBOLS)
    register50 = clean_symbols(register_symbols, limit=DEFAULT_REGISTER_LIMIT)

    if rotation_a_symbols is None or rotation_b_symbols is None:
        rotation_a_symbols, rotation_b_symbols = split_register_rotation(candidates100)

    rotation_a = clean_symbols(rotation_a_symbols, limit=DEFAULT_REGISTER_LIMIT)
    rotation_b = clean_symbols(rotation_b_symbols, limit=DEFAULT_REGISTER_LIMIT)

    candidate_attrs = (
        "monitor_symbols",
        "active_symbols",
        "candidate_push_symbols",
        "push_candidate_symbols",
        "push_symbols_100",
        "ats_candidate_targets",
        "register_candidate_symbols",
    )

    for attr in candidate_attrs:
        set_attr_safe(gd, attr, candidates100)

    register_attrs = (
        "push_symbols",
        "register_symbols",
        "ats_register_targets",
        "ats_targets",
        "push_register_symbols",
        "current_register_symbols",
    )

    for attr in register_attrs:
        set_attr_safe(gd, attr, register50)

    set_attr_safe(gd, "rotation_a_symbols", rotation_a)
    set_attr_safe(gd, "rotation_b_symbols", rotation_b)
    set_attr_safe(gd, "push_rotation_a_symbols", rotation_a)
    set_attr_safe(gd, "push_rotation_b_symbols", rotation_b)

    candidate_setters = (
        "set_monitor_symbols",
        "set_active_symbols",
    )
    for name in candidate_setters:
        call_setter_safe(gd, name, candidates100)

    register_setters = (
        "set_push_symbols",
        "set_register_symbols",
        "set_ats_register_targets",
        "set_ats_targets",
    )
    for name in register_setters:
        call_setter_safe(gd, name, register50)

    logger.info(
        "[PUSH SYMBOL BRIDGE] global_data injected candidate100=%d register50=%d "
        "rotation_A=%d rotation_B=%d candidate_head=%s register_head=%s",
        len(candidates100),
        len(register50),
        len(rotation_a),
        len(rotation_b),
        candidates100[:10],
        register50[:10],
    )


def runtime_set_key(fn: Callable[..., Any], key: str, value: Sequence[str]) -> bool:
    try:
        fn(key, list(value))
        return True
    except TypeError:
        try:
            fn(**{key: list(value)})
            return True
        except Exception:
            return False
    except Exception:
        return False


def set_push_stream_runtime_symbols(
    candidate_symbols: Sequence[str],
    register_symbols: Sequence[str],
    *,
    rotation_a_symbols: Optional[Sequence[str]] = None,
    rotation_b_symbols: Optional[Sequence[str]] = None,
) -> None:
    try:
        runtime_mod = importlib.import_module("trading.push.push_stream.runtime")
    except Exception:
        logger.warning("[PUSH SYMBOL BRIDGE] push_stream.runtime import failed")
        return

    candidates100 = clean_symbols(candidate_symbols, limit=DEFAULT_MAX_SYMBOLS)
    register50 = clean_symbols(register_symbols, limit=DEFAULT_REGISTER_LIMIT)

    if rotation_a_symbols is None or rotation_b_symbols is None:
        rotation_a_symbols, rotation_b_symbols = split_register_rotation(candidates100)

    rotation_a = clean_symbols(rotation_a_symbols, limit=DEFAULT_REGISTER_LIMIT)
    rotation_b = clean_symbols(rotation_b_symbols, limit=DEFAULT_REGISTER_LIMIT)

    payloads = {
        # 100件候補
        "monitor_symbols": candidates100,
        "active_symbols": candidates100,
        "candidate_push_symbols": candidates100,
        "push_candidate_symbols": candidates100,
        "push_symbols_100": candidates100,
        "ats_candidate_targets": candidates100,
        "register_candidate_symbols": candidates100,

        # 50件登録対象
        "push_symbols": register50,
        "register_symbols": register50,
        "ats_register_targets": register50,
        "ats_targets": register50,
        "push_register_symbols": register50,
        "current_register_symbols": register50,

        # rotation別
        "rotation_a_symbols": rotation_a,
        "rotation_b_symbols": rotation_b,
        "push_rotation_a_symbols": rotation_a,
        "push_rotation_b_symbols": rotation_b,
    }

    called = 0

    for name in (
        "set_runtime",
        "set_runtime_value",
        "_set_runtime",
    ):
        fn = getattr(runtime_mod, name, None)
        if not callable(fn):
            continue

        for k, v in payloads.items():
            if runtime_set_key(fn, k, v):
                called += 1

    fn_update = getattr(runtime_mod, "update_runtime", None)
    if callable(fn_update):
        try:
            fn_update(**payloads)
            called += 1
        except Exception:
            logger.debug(
                "[PUSH SYMBOL BRIDGE] runtime update_runtime failed",
                exc_info=True,
            )

    individual_setters = {
        "set_monitor_symbols": candidates100,
        "set_active_symbols": candidates100,
        "set_push_symbols": register50,
        "set_register_symbols": register50,
    }

    for name, value in individual_setters.items():
        fn = getattr(runtime_mod, name, None)
        if not callable(fn):
            continue
        try:
            fn(list(value))
            called += 1
        except Exception:
            logger.debug(
                "[PUSH SYMBOL BRIDGE] runtime setter failed name=%s",
                name,
                exc_info=True,
            )

    for k, v in payloads.items():
        try:
            setattr(runtime_mod, k, list(v))
        except Exception:
            pass

    logger.info(
        "[PUSH SYMBOL BRIDGE] push_stream.runtime injected candidate100=%d "
        "register50=%d rotation_A=%d rotation_B=%d setter_calls=%d "
        "candidate_head=%s register_head=%s",
        len(candidates100),
        len(register50),
        len(rotation_a),
        len(rotation_b),
        called,
        candidates100[:10],
        register50[:10],
    )


__all__ = [
    "set_global_data_symbols",
    "set_push_stream_runtime_symbols",
]
