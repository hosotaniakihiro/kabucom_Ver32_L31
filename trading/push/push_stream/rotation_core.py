# ============================================================
# File   : trading/push/push_stream/rotation_core.py
# Version: PRODUCTION-STABLE-REV8-PUSH-ROTATION-BACKOFF-KEEP-WS
# ------------------------------------------------------------
# PUSH登録制限50銘柄に対して、毎ターン固定銘柄を入れつつ、
# 残り枠をA/Bでローテーションする。
#
#   Aターン: 固定15 + A可変35
#   Bターン: 固定15 + B可変35
#
# REV8:
#   - REST register/unregister failure no longer retries every second.
#   - Failure triggers configurable backoff so PUSH DB receiving is prioritized.
#   - WebSocket is kept alive by default before REST registration.
#     Set PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER=1 to restore old close-before-register behavior.
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

from . import state, transport
from .rotation_logging import log_register_targets_with_names
from .rotation_register import run_one_batch_with_timeout
from .rotation_settings import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    REGISTER_TIMEOUT_SEC,
    ROTATE_HOLD_SEC,
    WS_WAIT_LOG_INTERVAL_SEC,
)
from .rotation_symbols import resolve_register_targets
from .transport import get_ws_sender, _is_ws_alive

try:
    from .protected_symbols import resolve_protected_push_symbols
except Exception:
    resolve_protected_push_symbols = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV8-PUSH-ROTATION-BACKOFF-KEEP-WS"


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
        return True
    if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}:
        return False
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def enable_rotation(enabled: bool = True) -> None:
    state._rotation_enabled = bool(enabled)
    logger.info("[push_stream] rotation enabled=%s", state._rotation_enabled)


def _sleep_or_stop(seconds: float) -> bool:
    end = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < end:
        if state._stop_event.is_set():
            return True
        time.sleep(min(0.1, max(0.0, end - time.monotonic())))
    return state._stop_event.is_set()


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        s = str(x).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _resolve_protected_safe() -> list[str]:
    try:
        if callable(resolve_protected_push_symbols):
            return _dedupe(list(resolve_protected_push_symbols()))
    except Exception:
        logger.exception("[push_stream] protected symbols resolve failed in rotation_core")
    return []


def _rotation_slot_sizes() -> tuple[int, int]:
    chunk = max(1, int(DEFAULT_REGISTER_CHUNK_SIZE or 50))
    fixed = max(0, _env_int("PUSH_ROTATION_FIXED_SYMBOLS", 15))
    fixed = min(fixed, chunk)
    variable_default = max(0, chunk - fixed)
    variable = max(0, _env_int("PUSH_ROTATION_VARIABLE_SYMBOLS", variable_default))
    variable = min(variable, max(0, chunk - fixed))
    return fixed, variable


def _build_protected_rotation_batches(targets: list[str]) -> tuple[list[str], list[str], list[str]]:
    targets = _dedupe([str(x).strip().upper() for x in targets])
    protected_all = _resolve_protected_safe()
    fixed_slots, variable_slots = _rotation_slot_sizes()
    chunk = max(1, int(DEFAULT_REGISTER_CHUNK_SIZE or 50))

    fixed = _dedupe(protected_all[:fixed_slots])
    fallback_enabled = _env_bool("PUSH_ROTATION_FIXED_FALLBACK_FROM_TARGETS", True)
    fallback_from_targets: list[str] = []

    if fallback_enabled and fixed_slots > 0 and len(fixed) < fixed_slots:
        need = fixed_slots - len(fixed)
        fixed_existing = set(fixed)
        fallback_from_targets = [x for x in targets if x not in fixed_existing][:need]
        fixed = _dedupe(fixed + fallback_from_targets)[:fixed_slots]

    fixed_set = set(fixed)
    protected_overflow = [x for x in protected_all[fixed_slots:] if x not in fixed_set]
    normal_from_targets = [x for x in targets if x not in fixed_set]
    variable_pool = _dedupe(protected_overflow + normal_from_targets)

    first = _dedupe(fixed + variable_pool[:variable_slots])[:chunk]
    second = _dedupe(fixed + variable_pool[variable_slots:variable_slots * 2])[:chunk]

    if fixed and not second and len(variable_pool) > 0:
        second = _dedupe(fixed + variable_pool[:variable_slots])[:chunk]

    logger.warning(
        "[push_stream] fixed/variable rotation batches fixed=%d variable_slots=%d protected_total=%d fixed_fallback=%d protected_overflow=%d variable_pool=%d A=%d B=%d head_fixed=%s headA=%s headB=%s",
        len(fixed),
        variable_slots,
        len(protected_all),
        len(fallback_from_targets),
        len(protected_overflow),
        len(variable_pool),
        len(first),
        len(second),
        fixed[:15],
        first[:15],
        second[:15],
    )
    return first, second, fixed


def _log_ws_not_ready_if_needed(*, ws_wait_count: int, last_ws_wait_log_ts: float) -> float:
    connected = state._connected_event.is_set()
    ws_alive = _is_ws_alive()
    now_ts = time.time()
    if ws_wait_count == 1 or now_ts - last_ws_wait_log_ts >= WS_WAIT_LOG_INTERVAL_SEC:
        logger.warning(
            "[push_stream] rotation waiting ws_not_ready connected_event=%s ws_alive=%s refresh_callable=%s sender_callable=%s wait_count=%d",
            connected,
            ws_alive,
            callable(state._refresh_callable),
            callable(get_ws_sender()),
            ws_wait_count,
        )
        return now_ts
    return last_ws_wait_log_ts


def _is_ws_ready() -> bool:
    try:
        return bool(state._connected_event.is_set() and _is_ws_alive())
    except Exception:
        return False


def _wait_ws_ready_after_register(label: str) -> bool:
    timeout = max(0.0, _env_float("PUSH_ROTATION_WAIT_WS_READY_AFTER_REGISTER_SEC", 4.0))
    settle = max(0.0, _env_float("PUSH_ROTATION_WAIT_WS_READY_AFTER_REGISTER_SETTLE_SEC", 0.25))
    if timeout <= 0:
        return True

    logger.info("[push_stream] rotation %s waiting WS ready after REST register timeout=%.3fs", label, timeout)
    end = time.monotonic() + timeout
    while time.monotonic() < end and not state._stop_event.is_set():
        if _is_ws_ready():
            if settle > 0:
                _sleep_or_stop(settle)
            logger.info("[push_stream] rotation %s WS ready after REST register -> hold can start settle=%.3fs", label, settle)
            return True
        time.sleep(0.05)

    logger.warning(
        "[push_stream] rotation %s WS not ready after REST register timeout=%.3fs connected_event=%s ws_alive=%s",
        label,
        timeout,
        state._connected_event.is_set(),
        _is_ws_alive(),
    )
    return False


def _close_ws_before_register(label: str) -> None:
    # With kabu Station auth instability, closing a live websocket before a REST
    # unregister/register attempt can destroy the only working PUSH feed.  Keep WS
    # alive by default; allow old behavior only by explicit env opt-in.
    if not _env_bool("PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER", False):
        logger.info(
            "[push_stream] rotation %s keep WS before REST register by env PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER=0",
            label,
        )
        return

    ws_app: Any = None
    try:
        with state._ws_state_lock:
            ws_app = getattr(state, "_ws_app", None)
    except Exception:
        ws_app = None

    try:
        state._connected_event.clear()
    except Exception:
        pass
    try:
        transport._clear_sender()
    except Exception:
        pass
    try:
        setattr(state, "_last_expected_ws_close_at", time.monotonic())
    except Exception:
        pass

    if ws_app is not None:
        logger.info(
            "[push_stream] rotation %s proactively closing WS before REST register to avoid vendor 10054 goodbye",
            label,
        )
        try:
            ws_app.close()
        except Exception:
            logger.debug("[push_stream] rotation %s ws close before register failed", label, exc_info=True)

    settle = max(0.0, _env_float("PUSH_ROTATION_WS_CLOSE_SETTLE_SEC", 0.15))
    if settle > 0:
        time.sleep(settle)


def _rotation_failure_backoff_seconds(failure_count: int) -> float:
    base = max(0.0, _env_float("PUSH_ROTATION_FAILURE_BACKOFF_SEC", 180.0))
    max_sec = max(base, _env_float("PUSH_ROTATION_FAILURE_BACKOFF_MAX_SEC", 600.0))
    multiplier = max(1.0, _env_float("PUSH_ROTATION_FAILURE_BACKOFF_MULTIPLIER", 1.5))
    n = max(0, int(failure_count) - 1)
    return min(max_sec, base * (multiplier ** n))


def _run_rotation_side(*, label: str, symbols: list[str], failure_count: int = 0) -> bool:
    if state._stop_event.is_set():
        return False
    if not symbols:
        logger.warning("[push_stream] rotation %s skipped: empty symbols", label)
        return False

    reason = f"rotation_{label}"
    log_register_targets_with_names(symbols, label=label, reason=reason)

    ok = False
    try:
        setattr(state, "_rotation_register_in_progress", True)
        logger.warning(
            "[push_stream] rotation %s guarded cycle rev8: keep_ws_default -> REST unregister/register -> wait_ws_ready -> hold failure_count=%d",
            label,
            failure_count,
        )
        _close_ws_before_register(label)
        ok = run_one_batch_with_timeout(label=label, symbols=symbols, timeout_sec=REGISTER_TIMEOUT_SEC)
    finally:
        try:
            setattr(state, "_rotation_register_in_progress", False)
        except Exception:
            pass

    if not ok:
        backoff = _rotation_failure_backoff_seconds(failure_count)
        logger.warning(
            "[push_stream] rotation %s register failed -> backoff %.1fs and keep same side size=%d failure_count=%d ws_alive=%s connected_event=%s",
            label,
            backoff,
            len(symbols),
            failure_count,
            _is_ws_alive(),
            state._connected_event.is_set(),
        )
        _sleep_or_stop(backoff)
        return False

    if not _wait_ws_ready_after_register(label):
        logger.warning(
            "[push_stream] rotation %s WS not ready after register -> retry same side without switching size=%d",
            label,
            len(symbols),
        )
        return False

    logger.info(
        "[push_stream] rotation %s hold start ok=%s hold=%.3fs size=%d ws_ready=True",
        label,
        ok,
        ROTATE_HOLD_SEC,
        len(symbols),
    )
    _sleep_or_stop(ROTATE_HOLD_SEC)
    return True


def _rotation_worker() -> None:
    fixed_slots, variable_slots = _rotation_slot_sizes()
    logger.info(
        "[push_stream] rotation worker started version=%s hold=%.3fs register_timeout=%.3fs chunk=%d fixed_slots=%d variable_slots=%d keep_ws_default=True failure_backoff=%.1fs",
        VERSION,
        ROTATE_HOLD_SEC,
        REGISTER_TIMEOUT_SEC,
        DEFAULT_REGISTER_CHUNK_SIZE,
        fixed_slots,
        variable_slots,
        _rotation_failure_backoff_seconds(1),
    )

    empty_count = 0
    ws_wait_count = 0
    last_ws_wait_log_ts = 0.0
    next_label = "A"
    failure_count = 0

    while not state._stop_event.is_set():
        try:
            if not state._rotation_enabled:
                time.sleep(1.0)
                continue

            if not state._connected_event.is_set() or not _is_ws_alive():
                ws_wait_count += 1
                last_ws_wait_log_ts = _log_ws_not_ready_if_needed(
                    ws_wait_count=ws_wait_count,
                    last_ws_wait_log_ts=last_ws_wait_log_ts,
                )
                _sleep_or_stop(2.0)
                continue

            ws_wait_count = 0
            targets = resolve_register_targets()
            if not targets:
                empty_count += 1
                if empty_count == 1 or empty_count % 15 == 0:
                    logger.warning("[push_stream] rotation waiting: no real targets empty_count=%d", empty_count)
                time.sleep(2.0)
                continue

            empty_count = 0
            first, second, fixed = _build_protected_rotation_batches(list(targets))
            logger.info(
                "[push_stream] rotation cycle targets=%d fixed=%d first=%d second=%d next=%s headA=%s headB=%s refresh_callable=%s ws_ready=%s failure_count=%d",
                len(targets),
                len(fixed),
                len(first),
                len(second),
                next_label,
                first[:10],
                second[:10],
                callable(state._refresh_callable),
                bool(state._connected_event.is_set() and _is_ws_alive()),
                failure_count,
            )

            if next_label == "A" or not second:
                ok = _run_rotation_side(label="A", symbols=list(first), failure_count=failure_count + 1)
                if ok:
                    failure_count = 0
                    next_label = "B" if second else "A"
                else:
                    failure_count += 1
                    next_label = "A"
                continue

            ok = _run_rotation_side(label="B", symbols=list(second), failure_count=failure_count + 1)
            if ok:
                failure_count = 0
                next_label = "A"
            else:
                failure_count += 1
                next_label = "B"

        except Exception:
            logger.exception("[push_stream] rotation worker loop failed; continue")
            time.sleep(1.0)

    logger.info("[push_stream] rotation worker stopped")


__all__ = ["VERSION", "enable_rotation", "_rotation_worker"]
