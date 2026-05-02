# ============================================================
# File   : trading/push/push_stream/force_rotation.py
# Version: PRODUCTION-STABLE-REV1.0-FORCE-A-B-ROTATION-THREAD
# ------------------------------------------------------------
# Purpose:
#   - 既存 rotation.py がA登録処理で詰まっても、
#     A/B 50銘柄の表示と切替だけは5秒ごとに必ず進める
#
# Design:
#   - push-force-rotation-worker という別スレッドで動く
#   - A面/B面の銘柄名を先に表示する
#   - 登録処理は別の短命スレッドへ投げる
#   - 前回登録処理がまだ残っている場合は新規登録投入をスキップ
#   - WebSocket ready に依存しない
#   - 実登録は既存の rotation.register_symbols() に委譲する
#
# Expected logs:
#   [push_force_rotation] worker started ...
#   [push_force_rotation] cycle side=A size=50
#   [PUSH ROTATION REGISTER TARGETS LINE] label=A reason=force_rotation_A ...
#   [push_force_rotation] dispatched side=A ...
#   [push_force_rotation] cycle side=B size=50
#   [PUSH ROTATION REGISTER TARGETS LINE] label=B reason=force_rotation_B ...
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from . import state
from .constants import DEFAULT_REGISTER_CHUNK_SIZE, DEFAULT_REGISTER_MAX_SYMBOLS
from .rotation import (
    _clean_symbol_list,
    _log_register_targets_with_names,
    _resolve_register_targets,
    register_symbols,
)

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV1.0-FORCE-A-B-ROTATION-THREAD"

FORCE_ROTATION_ENABLED = str(
    os.environ.get("PUSH_FORCE_ROTATION_ENABLED", "1")
).strip().lower() not in {"0", "false", "no", "off", "ng"}

FORCE_ROTATION_HOLD_SEC = float(
    os.environ.get("PUSH_FORCE_ROTATION_HOLD_SEC", "5.0")
)

FORCE_ROTATION_DISPATCH_GUARD_SEC = float(
    os.environ.get("PUSH_FORCE_ROTATION_DISPATCH_GUARD_SEC", "8.0")
)

FORCE_ROTATION_EMPTY_SLEEP_SEC = float(
    os.environ.get("PUSH_FORCE_ROTATION_EMPTY_SLEEP_SEC", "2.0")
)

_FORCE_THREAD_ATTR = "_force_rotate_thread"

_dispatch_lock = threading.RLock()
_dispatch_running = False
_dispatch_started_at: Optional[float] = None
_dispatch_side: Optional[str] = None


def _sleep_or_stop(seconds: float) -> bool:
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if state._stop_event.is_set():
            return True
        time.sleep(min(0.1, max(0.0, end - time.time())))
    return state._stop_event.is_set()


def _split_ab(symbols: Sequence[str]) -> Tuple[List[str], List[str]]:
    cleaned, _, _, _ = _clean_symbol_list(symbols)
    cleaned = cleaned[:DEFAULT_REGISTER_MAX_SYMBOLS]

    a = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]
    b = cleaned[DEFAULT_REGISTER_CHUNK_SIZE:DEFAULT_REGISTER_CHUNK_SIZE * 2]

    return a, b


def _dispatch_register_worker(
    *,
    side: str,
    symbols: Sequence[str],
    reason: str,
) -> None:
    global _dispatch_running, _dispatch_started_at, _dispatch_side

    try:
        logger.info(
            "[push_force_rotation] register worker start side=%s reason=%s size=%d",
            side,
            reason,
            len(symbols),
        )

        ok = register_symbols(
            symbols,
            force=True,
            reason=reason,
            label=side,
        )

        logger.info(
            "[push_force_rotation] register worker done side=%s reason=%s ok=%s size=%d",
            side,
            reason,
            ok,
            len(symbols),
        )

    except Exception:
        logger.exception(
            "[push_force_rotation] register worker failed side=%s reason=%s size=%d",
            side,
            reason,
            len(symbols),
        )

    finally:
        with _dispatch_lock:
            _dispatch_running = False
            _dispatch_started_at = None
            _dispatch_side = None


def _can_dispatch_new_register() -> bool:
    """
    前回の登録処理がまだ動いている場合、多重投入を避ける。

    ただし、一定秒数を超えて残っている場合は警告だけ出す。
    Python thread は安全にkillできないため、ここでは新規投入を抑制する。
    """
    with _dispatch_lock:
        if not _dispatch_running:
            return True

        elapsed = time.time() - float(_dispatch_started_at or time.time())

        logger.warning(
            "[push_force_rotation] previous register still running side=%s elapsed=%.3fs guard=%.3fs -> skip dispatch",
            _dispatch_side,
            elapsed,
            FORCE_ROTATION_DISPATCH_GUARD_SEC,
        )

        return False


def _dispatch_register_async(
    *,
    side: str,
    symbols: Sequence[str],
    reason: str,
) -> bool:
    global _dispatch_running, _dispatch_started_at, _dispatch_side

    cleaned, _, _, _ = _clean_symbol_list(symbols)
    cleaned = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

    if not cleaned:
        logger.warning(
            "[push_force_rotation] dispatch skipped empty side=%s reason=%s",
            side,
            reason,
        )
        return False

    if not callable(state._refresh_callable):
        logger.warning(
            "[push_force_rotation] dispatch skipped refresh_callable missing side=%s reason=%s size=%d",
            side,
            reason,
            len(cleaned),
        )
        return False

    if not _can_dispatch_new_register():
        return False

    with _dispatch_lock:
        _dispatch_running = True
        _dispatch_started_at = time.time()
        _dispatch_side = side

    th = threading.Thread(
        target=_dispatch_register_worker,
        name=f"push-force-register-{side}",
        daemon=True,
        kwargs={
            "side": side,
            "symbols": cleaned,
            "reason": reason,
        },
    )
    th.start()

    logger.info(
        "[push_force_rotation] dispatched side=%s reason=%s size=%d thread=%s",
        side,
        reason,
        len(cleaned),
        th.name,
    )
    return True


def _run_one_side(
    *,
    side: str,
    symbols: Sequence[str],
) -> None:
    cleaned, _, _, _ = _clean_symbol_list(symbols)
    cleaned = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

    if not cleaned:
        logger.warning("[push_force_rotation] side=%s skipped empty", side)
        return

    reason = f"force_rotation_{side}"

    # 重要:
    # 先に表示する。登録処理が詰まっても、A/B表示は必ず見える。
    _log_register_targets_with_names(
        cleaned,
        label=side,
        reason=reason,
    )

    logger.info(
        "[push_force_rotation] cycle side=%s size=%d hold=%.3fs connected=%s ws_alive=%s refresh_callable=%s",
        side,
        len(cleaned),
        FORCE_ROTATION_HOLD_SEC,
        state._connected_event.is_set(),
        _safe_ws_alive(),
        callable(state._refresh_callable),
    )

    _dispatch_register_async(
        side=side,
        symbols=cleaned,
        reason=reason,
    )


def _safe_ws_alive() -> bool:
    try:
        from .transport import _is_ws_alive

        return bool(_is_ws_alive())
    except Exception:
        return False


def force_rotation_worker() -> None:
    logger.info(
        "[push_force_rotation] worker started version=%s enabled=%s hold=%.3fs",
        VERSION,
        FORCE_ROTATION_ENABLED,
        FORCE_ROTATION_HOLD_SEC,
    )

    empty_count = 0

    while not state._stop_event.is_set():
        try:
            if not FORCE_ROTATION_ENABLED:
                time.sleep(1.0)
                continue

            if not bool(getattr(state, "_rotation_enabled", False)):
                logger.info("[push_force_rotation] waiting rotation_enabled=False")
                time.sleep(1.0)
                continue

            targets = _resolve_register_targets()
            first, second = _split_ab(targets)

            if not first and not second:
                empty_count += 1
                if empty_count == 1 or empty_count % 10 == 0:
                    logger.warning(
                        "[push_force_rotation] waiting no targets empty_count=%d",
                        empty_count,
                    )
                time.sleep(FORCE_ROTATION_EMPTY_SLEEP_SEC)
                continue

            empty_count = 0

            if first:
                _run_one_side(side="A", symbols=first)
                if _sleep_or_stop(FORCE_ROTATION_HOLD_SEC):
                    break

            if second:
                _run_one_side(side="B", symbols=second)
                if _sleep_or_stop(FORCE_ROTATION_HOLD_SEC):
                    break
            else:
                logger.warning(
                    "[push_force_rotation] B side empty targets=%d first=%d second=%d",
                    len(targets or []),
                    len(first),
                    len(second),
                )

        except Exception:
            logger.exception("[push_force_rotation] worker loop failed")
            time.sleep(2.0)

    logger.info("[push_force_rotation] worker stopped")


def start_force_rotation_thread() -> bool:
    """
    強制ローテーションスレッドを起動する。
    既に起動済みなら何もしない。
    """
    try:
        th = getattr(state, _FORCE_THREAD_ATTR, None)

        if th is not None and th.is_alive():
            logger.info("[push_force_rotation] thread already alive name=%s", th.name)
            return True

        th = threading.Thread(
            target=force_rotation_worker,
            name="push-force-rotation-worker",
            daemon=True,
        )
        setattr(state, _FORCE_THREAD_ATTR, th)
        th.start()

        logger.info("[push_force_rotation] thread started name=%s", th.name)
        return True

    except Exception:
        logger.exception("[push_force_rotation] start thread failed")
        return False


__all__ = [
    "force_rotation_worker",
    "start_force_rotation_thread",
]