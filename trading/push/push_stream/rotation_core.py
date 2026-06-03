# ============================================================
# File   : trading/push/push_stream/rotation_core.py
# Version: PRODUCTION-STABLE-REV3-PUSH-ROTATION-WAIT-WS-READY
# ------------------------------------------------------------
# PUSH A/B ローテーションの制御本体。
#
# Fix REV3:
#   - WS未接続のままHTTP refresh/registerを投げない。
#   - register失敗時に30秒holdせず、短い待機で次回リトライする。
#   - WinError 10054後の再接続中に「未登録状態でhold」する時間を減らす。
# ============================================================

from __future__ import annotations

import logging
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

VERSION = "PRODUCTION-STABLE-REV3-PUSH-ROTATION-WAIT-WS-READY"


def enable_rotation(enabled: bool = True) -> None:
    """PUSH A/B ローテーションの有効/無効を切り替える。"""
    state._rotation_enabled = bool(enabled)
    logger.info("[push_stream] rotation enabled=%s", state._rotation_enabled)


def _sleep_or_stop(seconds: float) -> bool:
    """stop_event を監視しながら短い粒度で sleep する。"""
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
        logger.exception('[push_stream] protected symbols resolve failed in rotation_core')
    return []


def _build_protected_rotation_batches(targets: list[str]) -> tuple[list[str], list[str], list[str]]:
    """
    A/B両面に protected symbols を入れる。
    """
    targets = _dedupe([str(x).strip().upper() for x in targets])
    protected = _resolve_protected_safe()

    if not protected:
        first = targets[:DEFAULT_REGISTER_CHUNK_SIZE]
        second = targets[DEFAULT_REGISTER_CHUNK_SIZE:DEFAULT_REGISTER_CHUNK_SIZE * 2]
        return first, second, []

    protected = protected[:max(0, DEFAULT_REGISTER_CHUNK_SIZE)]
    protected_set = set(protected)
    normal = [x for x in targets if x not in protected_set]

    normal_slots = max(0, DEFAULT_REGISTER_CHUNK_SIZE - len(protected))
    first = _dedupe(protected + normal[:normal_slots])
    second = _dedupe(protected + normal[normal_slots:normal_slots * 2])

    first = first[:DEFAULT_REGISTER_CHUNK_SIZE]
    second = second[:DEFAULT_REGISTER_CHUNK_SIZE]

    logger.warning(
        '[push_stream] protected rotation batches protected=%d normal=%d A=%d B=%d protected_symbols=%s headA=%s headB=%s',
        len(protected),
        len(normal),
        len(first),
        len(second),
        protected,
        first[:15],
        second[:15],
    )
    return first, second, protected


def _log_ws_not_ready_if_needed(
    *,
    ws_wait_count: int,
    last_ws_wait_log_ts: float,
) -> float:
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


def _run_rotation_side(*, label: str, symbols: list[str]) -> bool:
    """A面/B面の片側を登録し、登録成功時だけ指定秒数維持する。"""
    if state._stop_event.is_set():
        return False

    if not symbols:
        logger.warning("[push_stream] rotation %s skipped: empty symbols", label)
        return False

    reason = f"rotation_{label}"
    log_register_targets_with_names(symbols, label=label, reason=reason)

    ok = run_one_batch_with_timeout(
        label=label,
        symbols=symbols,
        timeout_sec=REGISTER_TIMEOUT_SEC,
    )

    if not ok:
        logger.warning(
            "[push_stream] rotation %s register failed -> short retry wait instead of hold size=%d",
            label,
            len(symbols),
        )
        _sleep_or_stop(2.0)
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
    """
    A/Bをローテーションする。REV3ではWS未接続中は登録を投げず待つ。
    """
    logger.info(
        "[push_stream] rotation worker started version=%s hold=%.3fs register_timeout=%.3fs",
        VERSION,
        ROTATE_HOLD_SEC,
        REGISTER_TIMEOUT_SEC,
    )

    empty_count = 0
    ws_wait_count = 0
    last_ws_wait_log_ts = 0.0

    while not state._stop_event.is_set():
        try:
            if not state._rotation_enabled:
                time.sleep(1.0)
                continue

            connected = state._connected_event.is_set()
            ws_alive = _is_ws_alive()

            if not connected or not ws_alive:
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
                        "[push_stream] rotation waiting: no real targets empty_count=%d hint=check runtime/global_data/active_symbol_manager/liquidity_guard and upstream candidates",
                        empty_count,
                    )
                time.sleep(2.0)
                continue

            empty_count = 0

            first, second, protected = _build_protected_rotation_batches(list(targets))

            logger.info(
                "[push_stream] rotation cycle targets=%d protected=%d first=%d second=%d headA=%s headB=%s refresh_callable=%s ws_ready=%s",
                len(targets),
                len(protected),
                len(first),
                len(second),
                first[:10],
                second[:10],
                callable(state._refresh_callable),
                True,
            )

            _run_rotation_side(label="A", symbols=list(first))
            if state._stop_event.is_set():
                break

            if second:
                # B面開始前にもWS状態を再確認する。
                if not state._connected_event.is_set() or not _is_ws_alive():
                    logger.warning("[push_stream] rotation B skipped because ws became not ready")
                    _sleep_or_stop(2.0)
                    continue
                _run_rotation_side(label="B", symbols=list(second))
            else:
                logger.warning(
                    "[push_stream] rotation second side empty; only A side active targets=%d",
                    len(targets),
                )

        except Exception:
            logger.exception("[push_stream] rotation worker loop failed; continue")
            time.sleep(1.0)

    logger.info("[push_stream] rotation worker stopped")


__all__ = [
    "VERSION",
    "enable_rotation",
    "_rotation_worker",
]
