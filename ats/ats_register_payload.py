# ============================================================
# File   : ats/ats_register_payload.py
# Version: Ver1.2-ATS-REGISTER-PAYLOAD-NO-CIRCULAR
# ------------------------------------------------------------
# register/unregister payload と vendor-style switch
# ✔ self import 排除
# ✔ ats_register_loop.py からの import に完全対応
# ============================================================

from __future__ import annotations

import logging
import time
from typing import List

from global_state import global_data

from .ats_api import request_api_put, is_in_429_cooldown
from .ats_register_state import (
    ATS_SYMBOL_BLACKLIST,
    REGISTER_LIMIT,
    sanitize_symbols,
    unique_keep_order,
    same_symbol_set,
    set_registered_symbols,
    get_registered_symbols,
    set_last_good_batch,
    get_last_good_batch,
    get_last_good_phase,
    set_active_phase,
)
from .ats_register_logging import (
    log_registered_symbols,
    save_ats_register_log,
    _print_current_ats_registered_symbols,
)

logger = logging.getLogger(__name__)

UNREGISTER_WAIT_SEC = 5.0


# ============================================================
# payload builder
# ============================================================

def build_ats_payload(symbols: List[str], limit: int = REGISTER_LIMIT):
    payload = []
    seen = set()

    flags_map = getattr(global_data, "symbol_flags", {})

    dropped_no_flags = 0
    dropped_bad_exchange = 0
    dropped_blacklist = 0

    symbols = sanitize_symbols(symbols)

    for s in symbols:
        s = str(s)

        if s in ATS_SYMBOL_BLACKLIST:
            dropped_blacklist += 1
            continue

        if s in seen:
            continue
        seen.add(s)

        flags = flags_map.get(s)
        if not flags:
            dropped_no_flags += 1
            continue

        exchange = flags.get("exchange", 1)
        try:
            exchange = int(exchange)
        except Exception:
            dropped_bad_exchange += 1
            continue

        if exchange not in (1, 2):
            dropped_bad_exchange += 1
            continue

        payload.append({"Symbol": s, "Exchange": exchange})
        if len(payload) >= limit:
            break

    if dropped_no_flags or dropped_bad_exchange or dropped_blacklist:
        logger.warning(
            "[ATS PAYLOAD] requested=%d built=%d dropped(no_flags=%d bad_exchange=%d blacklist=%d)",
            len(symbols),
            len(payload),
            dropped_no_flags,
            dropped_bad_exchange,
            dropped_blacklist,
        )

    return payload


# ============================================================
# vendor API wrappers
# ============================================================

def unregister_symbols(symbols: List[str]) -> bool:
    payload = build_ats_payload(symbols, limit=max(len(symbols), REGISTER_LIMIT))
    if not payload:
        logger.warning("[ATS UNREGISTER] payload empty")
        return False

    ok, status, body = request_api_put("/unregister", {"Symbols": payload}, timeout=8)
    if ok:
        logger.info("🧹 ATS UNREGISTER OK count=%d", len(payload))
        return True

    if status == 429:
        logger.warning("⚠ ATS UNREGISTER rate limited (429): %s", body)
    else:
        logger.error("❌ ATS UNREGISTER failed status=%s body=%s", status, body)
    return False


def unregister_all_vendor() -> bool:
    ok, status, body = request_api_put("/unregister/all", None, timeout=8)
    if ok:
        logger.info("🧹 ATS UNREGISTER ALL OK status=%s body=%s", status, body)
        set_registered_symbols([])
        set_active_phase("", [])
        return True

    if status == 429:
        logger.warning("⚠ ATS UNREGISTER ALL rate limited (429): %s", body)
    else:
        logger.error("❌ ATS UNREGISTER ALL failed status=%s body=%s", status, body)
    return False


def register_symbols_batch(symbols: List[str], phase: str) -> bool:
    payload = build_ats_payload(symbols, limit=REGISTER_LIMIT)
    if not payload:
        logger.warning("[ATS %s] register payload empty", phase)
        return False

    ok, status, body = request_api_put("/register", {"Symbols": payload}, timeout=8)
    if ok:
        logger.info("✅ ATS REGISTER success [%s] (%d symbols)", phase, len(payload))
        return True

    if status == 429:
        logger.warning(
            "⚠ ATS REGISTER rate limited [%s] body=%s symbols=%s",
            phase,
            body,
            [p["Symbol"] for p in payload],
        )
    else:
        logger.error(
            "❌ ATS REGISTER failed [%s] status=%s body=%s symbols=%s",
            phase,
            status,
            body,
            [p["Symbol"] for p in payload],
        )
    return False


def switch_batch_vendor_style(symbols: List[str], phase: str) -> bool:
    if is_in_429_cooldown():
        logger.warning("[ATS %s] switch skipped: in 429 cooldown", phase)
        return False

    logger.info("[ATS %s] unregister all start", phase)
    ok_unreg = unregister_all_vendor()
    if not ok_unreg:
        logger.warning("[ATS %s] unregister all failed", phase)
        return False

    logger.info("[ATS %s] wait after unregister all %.1fs", phase, UNREGISTER_WAIT_SEC)
    time.sleep(UNREGISTER_WAIT_SEC)

    logger.info("[ATS %s] register new batch start", phase)
    return register_symbols_batch(symbols, phase)


# ============================================================
# keep / restore last good
# ============================================================

def keep_last_good_batch(reason_phase: str) -> bool:
    last_symbols = get_last_good_batch()
    last_phase = get_last_good_phase()

    if not last_symbols:
        logger.warning("[ATS %s] no last_good_batch to keep", reason_phase)
        return False

    set_registered_symbols(last_symbols)
    set_active_phase(last_phase or reason_phase, last_symbols)

    logger.warning(
        "[ATS %s] keep last good in memory phase=%s size=%d",
        reason_phase,
        last_phase,
        len(last_symbols),
    )
    _print_current_ats_registered_symbols(last_symbols, phase=f"{reason_phase}-KEEP-LAST")
    return True


def restore_last_good_batch(reason_phase: str) -> bool:
    last_symbols = get_last_good_batch()
    last_phase = get_last_good_phase()

    if not last_symbols:
        logger.warning("[ATS %s] no last_good_batch to restore", reason_phase)
        return False

    logger.warning(
        "[ATS %s] restoring last good via vendor phase=%s size=%d",
        reason_phase,
        last_phase,
        len(last_symbols),
    )

    ok = switch_batch_vendor_style(last_symbols, phase=f"{reason_phase}-RESTORE")
    if not ok:
        logger.warning("[ATS %s] restore last good failed", reason_phase)
        return False

    set_registered_symbols(last_symbols)
    set_active_phase(last_phase or reason_phase, last_symbols)
    _print_current_ats_registered_symbols(last_symbols, phase=f"{reason_phase}-RESTORED")
    return True


# ============================================================
# high level register
# ============================================================

def register_payload(symbols: List[str], phase: str, apply_all_filters) -> bool:
    symbols = sanitize_symbols(apply_all_filters(symbols))
    payload = build_ats_payload(symbols, limit=REGISTER_LIMIT)

    if not payload:
        logger.warning("[ATS %s] payload empty -> skip", phase)
        return False

    symbols_only = unique_keep_order([p["Symbol"] for p in payload])
    current_registered = get_registered_symbols()

    if same_symbol_set(symbols_only, current_registered):
        logger.info(
            "[ATS %s] same payload as current -> keep registration (%d symbols)",
            phase,
            len(symbols_only),
        )
        set_registered_symbols(current_registered)
        set_active_phase(phase, current_registered)
        _print_current_ats_registered_symbols(current_registered, phase=f"{phase}-KEEP")
        return True

    log_registered_symbols(symbols_only, phase=phase)
    save_ats_register_log(symbols_only)

    ok = switch_batch_vendor_style(symbols_only, phase)
    if ok:
        set_registered_symbols(symbols_only)
        set_last_good_batch(symbols_only, phase)
        _print_current_ats_registered_symbols(symbols_only, phase=phase)
        return True

    logger.warning("[ATS %s] 50-symbol batch failed -> restore last good batch", phase)
    if restore_last_good_batch(reason_phase=phase):
        return True

    return keep_last_good_batch(reason_phase=phase)


__all__ = [
    "build_ats_payload",
    "unregister_symbols",
    "unregister_all_vendor",
    "register_symbols_batch",
    "switch_batch_vendor_style",
    "keep_last_good_batch",
    "restore_last_good_batch",
    "register_payload",
]