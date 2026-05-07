# ============================================================
# File   : data_collectors/push_runtime.py
# Version: DATA-COLLECTORS-PUSH-RUNTIME-V1
# ------------------------------------------------------------
# Purpose:
#   - PUSH受信本体を main.py から独立して起動する
#   - 株ステーションに銘柄登録してからPUSH受信を開始する
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


SUBSCRIPTION_START_CANDIDATES = [
    ("trading.push.subscription_manager", "start_symbol_subscription_manager"),
    ("trading.push.subscription_manager.core", "start_symbol_subscription_manager"),
    ("trading.push.subscription_manager", "force_refresh_subscriptions"),
    ("trading.push.subscription_manager.core", "force_refresh_subscriptions"),
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


def _call_with_fallback(fn, *args, **kwargs) -> Any:
    """
    既存関数の引数差分を吸収する。
    """
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
    """
    fn = resolve_callable(ROTATION_CONFIG_CANDIDATES, required=False)
    if fn is None:
        logger.warning(
            "[PUSH RUNTIME] rotation config function not found. "
            "既存 rotation.py 側の定数で 4.8/0.2 を設定してください."
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


def start_subscription_manager() -> bool:
    """
    PUSHを受けるため、株ステーションへ銘柄登録する処理を開始する。
    """
    fn = resolve_callable(SUBSCRIPTION_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[PUSH RUNTIME] no subscription start function resolved")
        return False

    logger.info("[PUSH RUNTIME] call subscription function: %s", fn)

    kwargs = {
        "target_total": PUSH_TARGET_TOTAL,
        "batch_size": PUSH_BATCH_SIZE,
        "register_seconds": PUSH_REGISTER_SEC,
        "switch_gap_seconds": PUSH_SWITCH_GAP_SEC,
        "reason": "data_collectors_push_receiver",
    }

    try:
        result = _call_with_fallback(fn, **kwargs)
        logger.info("[PUSH RUNTIME] subscription start returned: %r", result)
        return True
    except Exception:
        logger.exception("[PUSH RUNTIME] subscription start failed")
        return False


def start_push_stream() -> bool:
    """
    PUSH WebSocket / PUSH受信本体を開始する。
    """
    fn = resolve_callable(PUSH_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[PUSH RUNTIME] no push start function resolved")
        return False

    logger.info("[PUSH RUNTIME] call push stream function: %s", fn)

    try:
        result = _call_with_fallback(fn)
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

    os.environ.setdefault("PUSH_REGISTER_SEC", str(PUSH_REGISTER_SEC))
    os.environ.setdefault("PUSH_SWITCH_GAP_SEC", str(PUSH_SWITCH_GAP_SEC))
    os.environ.setdefault("PUSH_BATCH_SIZE", str(PUSH_BATCH_SIZE))
    os.environ.setdefault("PUSH_TARGET_TOTAL", str(PUSH_TARGET_TOTAL))

    configure_push_rotation_if_supported()

    sub_ok = start_subscription_manager()
    push_ok = start_push_stream()

    if not sub_ok:
        logger.error("[PUSH RUNTIME] subscription manager could not start")
    if not push_ok:
        logger.error("[PUSH RUNTIME] push stream could not start")

    if not sub_ok and not push_ok:
        logger.error("[PUSH RUNTIME] abort because both subscription and push stream failed")
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
                subscription_started=sub_ok,
                push_started=push_ok,
                register_seconds=PUSH_REGISTER_SEC,
                switch_gap_seconds=PUSH_SWITCH_GAP_SEC,
                batch_size=PUSH_BATCH_SIZE,
                target_total=PUSH_TARGET_TOTAL,
            )
            logger.info("[PUSH RUNTIME] heartbeat alive")
            last_hb = now

        time.sleep(1.0)
