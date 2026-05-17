# ============================================================
# File   : trading/push/push_stream/rotation_core.py
# Version: PRODUCTION-STABLE-REV2-PUSH-ROTATION-PROTECTED-BOTH-SIDES
# ------------------------------------------------------------
# PUSH A/B 50銘柄ローテーションの制御本体。
#
# Default flow:
#   A面50銘柄登録 -> 4.8秒維持 -> 全解除 -> 0.2秒待機 ->
#   B面50銘柄登録 -> 4.8秒維持 -> 全解除 -> 0.2秒待機 -> 繰り返し
#
# Ver2:
#   - 保有中 / 未約定 / 直近ENTRY候補の protected symbols を
#     A面/B面の両方へ入れる。
#   - これにより、売買中銘柄がB面中にPUSH未登録になる問題を減らす。
#
# Notes:
#   - 銘柄解決は rotation_symbols.py に委譲
#   - 登録処理は rotation_register.py に委譲
#   - ログ処理は rotation_logging.py に委譲
#   - 旧 rotation.py は互換APIとして残す
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
    UNREGISTER_TO_REGISTER_WAIT_SEC,
    WS_WAIT_LOG_INTERVAL_SEC,
)
from .rotation_symbols import resolve_register_targets
from .transport import get_ws_sender, _is_ws_alive

try:
    from .protected_symbols import resolve_protected_push_symbols
except Exception:
    resolve_protected_push_symbols = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV2-PUSH-ROTATION-PROTECTED-BOTH-SIDES"


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

    例:
      protected=8銘柄, chunk=50 の場合
        A = protected 8 + normal 42
        B = protected 8 + normal 次42

    これにより、保有中/未約定銘柄はA面でもB面でも登録される。
    """
    targets = _dedupe([str(x).strip().upper() for x in targets])
    protected = _resolve_protected_safe()

    if not protected:
        first = targets[:DEFAULT_REGISTER_CHUNK_SIZE]
        second = targets[DEFAULT_REGISTER_CHUNK_SIZE:DEFAULT_REGISTER_CHUNK_SIZE * 2]
        return first, second, []

    # protected は両面へ入れる。通常枠からは除外して重複を避ける。
    protected = protected[:max(0, DEFAULT_REGISTER_CHUNK_SIZE)]
    protected_set = set(protected)
    normal = [x for x in targets if x not in protected_set]

    normal_slots = max(0, DEFAULT_REGISTER_CHUNK_SIZE - len(protected))
    first = _dedupe(protected + normal[:normal_slots])
    second = _dedupe(protected + normal[normal_slots:normal_slots * 2])

    # B側がprotectedだけになるケースでも、保護目的なので登録する。
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
            "[push_stream] rotation ws_not_ready but continue HTTP refresh "
            "connected_event=%s ws_alive=%s refresh_callable=%s sender_callable=%s wait_count=%d",
            connected,
            ws_alive,
            callable(state._refresh_callable),
            callable(get_ws_sender()),
            ws_wait_count,
        )
        return now_ts

    return last_ws_wait_log_ts


def _run_rotation_side(*, label: str, symbols: list[str]) -> None:
    """A面/B面の片側を登録し、指定秒数だけ維持する。"""
    if state._stop_event.is_set():
        return

    if not symbols:
        logger.warning("[push_stream] rotation %s skipped: empty symbols", label)
        return

    reason = f"rotation_{label}"
    log_register_targets_with_names(symbols, label=label, reason=reason)

    ok = run_one_batch_with_timeout(
        label=label,
        symbols=symbols,
        timeout_sec=REGISTER_TIMEOUT_SEC,
    )

    logger.info(
        "[push_stream] rotation %s hold start ok=%s hold=%.3fs size=%d",
        label,
        ok,
        ROTATE_HOLD_SEC,
        len(symbols),
    )

    _sleep_or_stop(ROTATE_HOLD_SEC)


def _rotation_worker() -> None:
    """
    A/B 50銘柄を 4.8秒登録維持 + 0.2秒解除待機 でローテーションする。

    実際の全解除 + 0.2秒待機 + 登録は run_one_batch_with_timeout()
    -> rotation_register.py -> 旧 rotation.register_symbols()
    -> subscription_manager.refresh_subscriptions(clear_first=True) で行う。
    """
    logger.info(
        "[push_stream] rotation worker started version=%s hold=%.3fs unregister_wait=%.3fs register_timeout=%.3fs",
        VERSION,
        ROTATE_HOLD_SEC,
        UNREGISTER_TO_REGISTER_WAIT_SEC,
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
            else:
                ws_wait_count = 0

            targets = resolve_register_targets()

            if not targets:
                empty_count += 1
                if empty_count == 1 or empty_count % 15 == 0:
                    logger.warning(
                        "[push_stream] rotation waiting: no real targets empty_count=%d "
                        "hint=check runtime/global_data/active_symbol_manager/liquidity_guard and upstream candidates",
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
                bool(connected and ws_alive),
            )

            _run_rotation_side(label="A", symbols=list(first))
            if state._stop_event.is_set():
                break

            if second:
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
