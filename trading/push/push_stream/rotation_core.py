# ============================================================
# File   : trading/push/push_stream/rotation_core.py
# Version: PRODUCTION-STABLE-REV6.2-PUSH-FIXED-FALLBACK-FROM-TARGETS
# ------------------------------------------------------------
# PUSH登録制限50銘柄に対して、毎ターン固定銘柄を入れつつ、
# 残り枠をA/Bでローテーションする。
#
#   Aターン: 固定15 + A可変35
#   Bターン: 固定15 + B可変35
#
# User design:
#   A register -> hold -> unregister_all -> wait -> B register
#   B register -> hold -> unregister_all -> wait -> A register
#
# ENV:
#   PUSH_ROTATION_FIXED_SYMBOLS=15
#   PUSH_ROTATION_VARIABLE_SYMBOLS=35
#
# REV6.2:
#   - protected/open-position symbols が空でも fixed=0 にしない。
#   - PUSH_ROTATION_FIXED_FALLBACK_FROM_TARGETS=1 の場合、targets先頭から
#     固定15枠を作り、A/B両ターンに必ず固定枠として入れる。
# ============================================================

from __future__ import annotations

import logging
import os
import time

from . import state
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

VERSION = "PRODUCTION-STABLE-REV6.2-PUSH-FIXED-FALLBACK-FROM-TARGETS"


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
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if state._stop_event.is_set():
            return True
        time.sleep(min(0.1, max(0.0, end - time.time())))
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
    """
    50銘柄制限内で、固定枠 + 可変枠を作る。

    例: chunk=50, fixed=15, variable=35
      A = fixed15 + normal[0:35]
      B = fixed15 + normal[35:70]

    protected が15を超える場合:
      - 先頭15だけを毎ターン固定にする
      - 残りのprotectedは normal 側の先頭へ戻し、A/B可変枠で回す

    protected が空の場合:
      - targets先頭から固定枠を作る。
      - これにより fixed=0 / A=50 / B=50 ではなく、固定15 + 可変35になる。
    """
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

    # protectedの16番目以降も捨てず、可変枠の先頭へ入れる。
    protected_overflow = [x for x in protected_all[fixed_slots:] if x not in fixed_set]
    normal_from_targets = [x for x in targets if x not in fixed_set]
    variable_pool = _dedupe(protected_overflow + normal_from_targets)

    first = _dedupe(fixed + variable_pool[:variable_slots])[:chunk]
    second = _dedupe(fixed + variable_pool[variable_slots:variable_slots * 2])[:chunk]

    # B側が足りない場合でも、Aと重複しすぎない範囲で後続を入れる。完全空ならAだけで回す。
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


def _ws_stable_age_sec() -> float | None:
    try:
        last_connect = getattr(state, "_last_connect_at", None)
        if last_connect is None:
            return None
        return max(0.0, (getattr(__import__("datetime"), "datetime").datetime.now() - last_connect.replace(tzinfo=None)).total_seconds())
    except Exception:
        return None


def _wait_for_stable_ws_before_register(label: str) -> bool:
    """
    reconnect直後にregister/unregisterを入れると、kabu Station 側から
    WinError 10054 で切られやすい。接続直後は少し待ってからregisterする。
    grace=0 の場合は、接続確認だけ行って即registerする。
    """
    grace = max(0.0, _env_float("PUSH_ROTATION_WS_STABLE_GRACE_SEC", 3.0))
    deadline_extra = max(1.0, _env_float("PUSH_ROTATION_WS_STABLE_MAX_WAIT_SEC", 8.0))
    deadline = time.time() + deadline_extra

    while not state._stop_event.is_set():
        if not state._connected_event.is_set() or not _is_ws_alive():
            logger.warning("[push_stream] rotation %s wait stable postponed: ws not ready; retry same side", label)
            return False

        age = _ws_stable_age_sec()
        if age is None or age >= grace:
            return True

        remain = grace - age
        if time.time() >= deadline:
            logger.warning(
                "[push_stream] rotation %s ws stable wait timeout age=%.2fs grace=%.2fs -> continue",
                label,
                age,
                grace,
            )
            return True

        logger.info(
            "[push_stream] rotation %s waiting ws stable age=%.2fs grace=%.2fs remain=%.2fs",
            label,
            age,
            grace,
            remain,
        )
        _sleep_or_stop(min(0.5, max(0.1, remain)))

    return False


def _run_rotation_side(*, label: str, symbols: list[str]) -> bool:
    if state._stop_event.is_set():
        return False
    if not symbols:
        logger.warning("[push_stream] rotation %s skipped: empty symbols", label)
        return False

    if not _wait_for_stable_ws_before_register(label):
        _sleep_or_stop(1.0)
        return False

    reason = f"rotation_{label}"
    log_register_targets_with_names(symbols, label=label, reason=reason)
    ok = run_one_batch_with_timeout(label=label, symbols=symbols, timeout_sec=REGISTER_TIMEOUT_SEC)

    if not ok:
        logger.warning(
            "[push_stream] rotation %s register failed -> retry same side without switching size=%d",
            label,
            len(symbols),
        )
        _sleep_or_stop(1.0)
        return False

    logger.info(
        "[push_stream] rotation %s hold start ok=%s hold=%.3fs size=%d",
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
        "[push_stream] rotation worker started version=%s hold=%.3fs register_timeout=%.3fs chunk=%d fixed_slots=%d variable_slots=%d stable_grace=%.3fs",
        VERSION,
        ROTATE_HOLD_SEC,
        REGISTER_TIMEOUT_SEC,
        DEFAULT_REGISTER_CHUNK_SIZE,
        fixed_slots,
        variable_slots,
        _env_float("PUSH_ROTATION_WS_STABLE_GRACE_SEC", 3.0),
    )

    empty_count = 0
    ws_wait_count = 0
    last_ws_wait_log_ts = 0.0
    next_label = "A"

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
                    logger.warning(
                        "[push_stream] rotation waiting: no real targets empty_count=%d",
                        empty_count,
                    )
                time.sleep(2.0)
                continue

            empty_count = 0
            first, second, fixed = _build_protected_rotation_batches(list(targets))
            logger.info(
                "[push_stream] rotation cycle targets=%d fixed=%d first=%d second=%d next=%s headA=%s headB=%s refresh_callable=%s ws_ready=%s",
                len(targets),
                len(fixed),
                len(first),
                len(second),
                next_label,
                first[:10],
                second[:10],
                callable(state._refresh_callable),
                bool(state._connected_event.is_set() and _is_ws_alive()),
            )

            if next_label == "A" or not second:
                ok = _run_rotation_side(label="A", symbols=list(first))
                next_label = "B" if ok and second else "A"
                continue

            # Bだけを特別扱いしない。ここでwsが落ちても _run_rotation_side がFalseを返し、
            # next_label はBのまま維持されるため、再接続後にBから再開する。
            ok = _run_rotation_side(label="B", symbols=list(second))
            next_label = "A" if ok else "B"

        except Exception:
            logger.exception("[push_stream] rotation worker loop failed; continue")
            time.sleep(1.0)

    logger.info("[push_stream] rotation worker stopped")


__all__ = ["VERSION", "enable_rotation", "_rotation_worker"]
