# ============================================================
# File   : data_collectors/push_runtime.py
# Version: DATA-COLLECTORS-PUSH-RUNTIME-V2-ENABLE-ROTATION
# ------------------------------------------------------------
# Purpose:
#   - PUSH受信本体を main.py から独立して起動する
#   - PUSH A/B 50銘柄ローテーションを明示的に有効化する
#   - rotation worker から subscription_manager.refresh_subscriptions を呼び、
#     株ステーションへの登録を実行する
# ============================================================

from __future__ import annotations

import logging
import os
import schedule
import time
from typing import Any

from data_collectors.config import (
    PUSH_BATCH_SIZE,
    PUSH_REGISTER_SEC,
    PUSH_SWITCH_GAP_SEC,
    PUSH_TARGET_TOTAL,
)
from data_collectors.heartbeat import write_heartbeat
from data_collectors.import_resolver import resolve_callable

logger = logging.getLogger(__name__)


SUBSCRIPTION_REFRESH_CANDIDATES = [
    ("trading.push.subscription_manager", "refresh_subscriptions"),
    ("trading.push.subscription_manager.core", "refresh_subscriptions"),
    ("trading.push.subscription_manager", "force_refresh_subscriptions"),
    ("trading.push.subscription_manager.core", "force_refresh_subscriptions"),
]

SUBSCRIPTION_MANAGER_START_CANDIDATES = [
    ("trading.push.subscription_manager", "start_symbol_subscription_manager"),
    ("trading.push.subscription_manager.core", "start_symbol_subscription_manager"),
    ("trading.push.symbol_subscription_manager", "start_symbol_subscription_manager"),
]

PUSH_START_CANDIDATES = [
    ("trading.push.push_stream.runtime", "start_push_stream"),
    ("trading.push.push_stream.runtime", "start"),
    ("trading.push.push_stream", "start_push_stream"),
    ("trading.push.push_stream", "start"),
    ("trading.push.push_stream.core", "start_push_stream"),
]

ROTATION_CONFIG_CANDIDATES = [
    ("trading.push.push_stream.rotation", "set_rotation_timing"),
    ("trading.push.push_stream.rotation", "configure_rotation"),
]


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _call_with_fallback(fn, *args, **kwargs) -> Any:
    """既存関数の引数差分を吸収する。"""
    try:
        return fn(*args, **kwargs)
    except TypeError:
        pass

    try:
        return fn()
    except TypeError:
        pass

    try:
        return fn(schedule)
    except TypeError:
        pass

    return fn(*args, **kwargs)


def configure_push_rotation_if_supported() -> None:
    """
    既存側にローテーション設定関数があれば、
    4.8秒登録 + 0.2秒切替ギャップを渡す。

    Ver32_L31 では rotation_core / rotation_settings 側で既に
    4.8 / 0.2 が反映済みのため、関数が無くても致命ではない。
    """
    fn = resolve_callable(ROTATION_CONFIG_CANDIDATES, required=False)
    if fn is None:
        logger.info(
            "[PUSH RUNTIME] rotation config function not found; "
            "use existing rotation_settings defaults/env."
        )
        return

    kwargs = {
        "register_seconds": PUSH_REGISTER_SEC,
        "switch_gap_seconds": PUSH_SWITCH_GAP_SEC,
        "batch_size": PUSH_BATCH_SIZE,
        "target_total": PUSH_TARGET_TOTAL,
    }

    try:
        logger.info("[PUSH RUNTIME] configure rotation kwargs=%s", kwargs)
        _call_with_fallback(fn, **kwargs)
    except Exception:
        logger.exception("[PUSH RUNTIME] configure rotation failed")


def resolve_subscription_refresh_callable():
    """
    push_stream rotation worker が呼ぶ登録更新関数を解決する。
    start_symbol_subscription_manager の60秒background_loopではなく、
    rotation_A / rotation_B から直接 refresh_subscriptions を呼ぶ。
    """
    return resolve_callable(SUBSCRIPTION_REFRESH_CANDIDATES, required=False)


def start_subscription_manager_if_requested() -> bool:
    """
    互換用。
    通常は rotation worker が登録を行うため、60秒 background_loop は起動しない。
    必要な場合だけ DATA_COLLECTORS_START_SUB_MANAGER_LOOP=1 で有効化する。
    """
    if not _env_bool("DATA_COLLECTORS_START_SUB_MANAGER_LOOP", False):
        logger.info("[PUSH RUNTIME] subscription manager background loop skipped; rotation worker handles registration")
        return True

    fn = resolve_callable(SUBSCRIPTION_MANAGER_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[PUSH RUNTIME] no subscription manager start function resolved")
        return False

    logger.info("[PUSH RUNTIME] call subscription manager loop function: %s", fn)

    try:
        result = _call_with_fallback(fn)
        logger.info("[PUSH RUNTIME] subscription manager loop start returned: %r", result)
        return True
    except Exception:
        logger.exception("[PUSH RUNTIME] subscription manager loop start failed")
        return False


def start_push_stream() -> bool:
    """PUSH WebSocket / PUSH受信本体を開始し、rotationを明示的に有効化する。"""
    fn = resolve_callable(PUSH_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[PUSH RUNTIME] no push start function resolved")
        return False

    refresh_callable = resolve_subscription_refresh_callable()

    logger.info(
        "[PUSH RUNTIME] call push stream function=%s enable_rotate=True refresh_callable=%s",
        fn,
        bool(callable(refresh_callable)),
    )

    try:
        result = _call_with_fallback(
            fn,
            refresh_callable=refresh_callable,
            enable_rotate=True,
        )
        logger.info("[PUSH RUNTIME] push stream start returned: %r", result)
        return True
    except Exception:
        logger.exception("[PUSH RUNTIME] push stream start failed")
        return False


def run_forever() -> int:
    logger.info("[PUSH RUNTIME] START")
    logger.info(
        "[PUSH RUNTIME] rotation target_total=%s batch_size=%s register_sec=%s gap_sec=%s",
        PUSH_TARGET_TOTAL,
        PUSH_BATCH_SIZE,
        PUSH_REGISTER_SEC,
        PUSH_SWITCH_GAP_SEC,
    )

    # 既存 rotation_settings が import 時に読む可能性があるため、先に注入する。
    os.environ.setdefault("PUSH_REGISTER_SEC", str(PUSH_REGISTER_SEC))
    os.environ.setdefault("PUSH_SWITCH_GAP_SEC", str(PUSH_SWITCH_GAP_SEC))
    os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", str(PUSH_REGISTER_SEC))
    os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", str(PUSH_SWITCH_GAP_SEC))
    os.environ.setdefault("PUSH_BATCH_SIZE", str(PUSH_BATCH_SIZE))
    os.environ.setdefault("PUSH_TARGET_TOTAL", str(PUSH_TARGET_TOTAL))

    configure_push_rotation_if_supported()

    # 重要:
    #   先に 60秒 background_loop を動かすと reason=background_loop で登録され、
    #   4.8秒A/Bローテーションにならない。
    #   そのため通常は push_stream rotation worker に登録を任せる。
    sub_loop_ok = start_subscription_manager_if_requested()
    push_ok = start_push_stream()

    if not sub_loop_ok:
        logger.error("[PUSH RUNTIME] subscription manager background loop could not start")
    if not push_ok:
        logger.error("[PUSH RUNTIME] push stream could not start")

    if not push_ok:
        logger.error("[PUSH RUNTIME] abort because push stream failed")
        return 1

    last_hb = 0.0

    while True:
        try:
            schedule.run_pending()
        except Exception:
            logger.exception("[PUSH RUNTIME] schedule.run_pending failed")

        now = time.time()
        if now - last_hb >= 30:
            write_heartbeat(
                "push_receiver",
                status="alive",
                subscription_loop_started=sub_loop_ok,
                push_started=push_ok,
                rotation_enabled=True,
                register_seconds=PUSH_REGISTER_SEC,
                switch_gap_seconds=PUSH_SWITCH_GAP_SEC,
                batch_size=PUSH_BATCH_SIZE,
                target_total=PUSH_TARGET_TOTAL,
            )
            logger.info("[PUSH RUNTIME] heartbeat alive")
            last_hb = now

        time.sleep(1.0)
