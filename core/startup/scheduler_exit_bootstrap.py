# ============================================================
# File   : core/startup/scheduler_exit_bootstrap.py
# Version: FINAL-PRODUCTION-REV1.3-EXIT-NO-BROKER-EMPTY-HARD-SKIP
# ------------------------------------------------------------
# 【概要】
#   EXIT order sender 接続と EXIT loop scheduler 登録。
#
# REV1.3:
#   - broker authoritative empty だけで exit_loop_5s を完全skipしない。
#   - 2026-06-03ログで、実建玉/ローカル状態の復元前に
#       [EXIT SCHEDULER] empty fast skip reason=broker_authoritative_empty
#     が5秒ごとに出続け、exit_loop自体が起動しないためイグジット不能になっていた。
#   - broker empty は軽量skipの根拠としては使わず、直近 exit_loop が空確認した場合のみ
#     EXIT_EMPTY_FAST_SKIP_TTL_SEC の短いTTLでskipする。
#   - どうしても旧挙動に戻したい場合のみ EXIT_BROKER_EMPTY_IMMEDIATE_SKIP=1 を明示する。
#
# REV1.2:
#   - open_position_sync_throttle_patch が設定する実際の属性名
#       open_positions_source_mode
#       open_positions_broker_read_ok
#       open_positions_synced_count
#     を見て、broker authoritative empty なら exit_loop_5s を初回から即skip
#   - これにより建玉なしでも18秒走って previous still running になる問題を抑止
#   - 建玉ありそうなglobal状態があれば従来通りexit_loopを実行
#
# REV1.1:
#   - 建玉なし確認後、短いTTLだけexit_loopをスキップ
#
# ENV:
#   EXIT_EMPTY_FAST_SKIP_ENABLED=1
#   EXIT_EMPTY_FAST_SKIP_TTL_SEC=10
#   EXIT_BROKER_EMPTY_IMMEDIATE_SKIP=0   # REV1.3 default: disabled
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

import schedule

from global_state import global_data
from core.startup.scheduler_helpers import has_schedule_tag, log_scheduler_snapshot
from core.startup.scheduler_market_guard import is_market_time_for_exit

logger = logging.getLogger(__name__)

_EMPTY_CONFIRMED_AT_TS: float | None = None
_LAST_STARTED_TS: float | None = None
_LAST_BROKER_EMPTY_LOG_TS: float = 0.0


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enable", "enabled", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled", "ng"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return max(0.0, float(raw))
    except Exception:
        pass
    return float(default)


def _as_len(v: Any) -> int:
    try:
        if v is None:
            return 0
        if isinstance(v, dict):
            return len(v)
        if isinstance(v, (list, tuple, set)):
            return len(v)
        if hasattr(v, "empty"):
            return 0 if bool(getattr(v, "empty")) else len(v)
        return int(len(v))
    except Exception:
        return 0


def _global_has_open_positions_hint() -> bool:
    """軽いglobal状態だけで建玉ありそうか確認する。重いbroker/API/DBは叩かない。"""
    try:
        for name in (
            "open_positions",
            "open_position_map",
            "active_positions",
            "position_cache",
            "open_position_cache",
            "current_positions",
            "positions",
            "runtime_positions",
            "entry_positions",
        ):
            if _as_len(getattr(global_data, name, None)) > 0:
                logger.debug("[EXIT SCHEDULER] open position hint attr=%s len>0", name)
                return True
    except Exception:
        pass
    return False


def _broker_authoritative_empty_hint() -> bool:
    """open_position_sync_throttle_patch の broker empty キャッシュを確認する。

    REV1.3では、これはログ/旧互換用のヒントに留める。
    デフォルトではこの結果だけで exit_loop_5s をskipしない。
    """
    if not _env_bool("EXIT_BROKER_EMPTY_IMMEDIATE_SKIP", False):
        return False
    if _global_has_open_positions_hint():
        return False
    try:
        mode = str(getattr(global_data, "open_positions_source_mode", "") or "")
        read_ok = bool(getattr(global_data, "open_positions_broker_read_ok", False))
        cnt = int(getattr(global_data, "open_positions_synced_count", 0) or 0)
        if read_ok and cnt == 0 and mode.startswith("broker_credit_authoritative_empty"):
            return True
    except Exception:
        pass

    # 互換: 旧名が設定されている環境も見る。
    try:
        old_empty = bool(getattr(global_data, "open_position_broker_authoritative_empty", False))
        old_until = getattr(global_data, "open_position_broker_authoritative_empty_until", None)
        if old_empty:
            if old_until is None:
                return True
            try:
                return float(old_until) > time.time()
            except Exception:
                return True
    except Exception:
        pass
    return False


def _broker_empty_observed_for_log() -> bool:
    """skipには使わず、状態把握ログだけに使う。"""
    if _global_has_open_positions_hint():
        return False
    try:
        mode = str(getattr(global_data, "open_positions_source_mode", "") or "")
        read_ok = bool(getattr(global_data, "open_positions_broker_read_ok", False))
        cnt = int(getattr(global_data, "open_positions_synced_count", 0) or 0)
        if read_ok and cnt == 0 and mode.startswith("broker_credit_authoritative_empty"):
            return True
    except Exception:
        pass
    try:
        return bool(getattr(global_data, "open_position_broker_authoritative_empty", False))
    except Exception:
        return False


def _maybe_log_broker_empty_observed() -> None:
    global _LAST_BROKER_EMPTY_LOG_TS
    try:
        if not _broker_empty_observed_for_log():
            return
        now = time.time()
        interval = max(5.0, _env_float("EXIT_BROKER_EMPTY_OBSERVED_LOG_INTERVAL_SEC", 30.0))
        if (now - _LAST_BROKER_EMPTY_LOG_TS) < interval:
            return
        _LAST_BROKER_EMPTY_LOG_TS = now
        logger.warning(
            "[EXIT SCHEDULER] broker empty observed but not hard-skipping exit_loop immediate_skip=%s mode=%s read_ok=%s cnt=%s rev=1.3",
            os.getenv("EXIT_BROKER_EMPTY_IMMEDIATE_SKIP", "0"),
            getattr(global_data, "open_positions_source_mode", ""),
            getattr(global_data, "open_positions_broker_read_ok", None),
            getattr(global_data, "open_positions_synced_count", None),
        )
    except Exception:
        pass


def _empty_fast_skip_active() -> bool:
    if not _env_bool("EXIT_EMPTY_FAST_SKIP_ENABLED", True):
        return False
    if _global_has_open_positions_hint():
        return False
    # REV1.3: broker empty だけで即skipしない。旧挙動は環境変数で明示した時だけ。
    if _broker_authoritative_empty_hint():
        return True
    if _EMPTY_CONFIRMED_AT_TS is None:
        return False
    ttl = _env_float("EXIT_EMPTY_FAST_SKIP_TTL_SEC", 10.0)
    return (time.time() - float(_EMPTY_CONFIRMED_AT_TS)) < ttl


def _mark_empty_confirmed() -> None:
    global _EMPTY_CONFIRMED_AT_TS
    _EMPTY_CONFIRMED_AT_TS = time.time()
    try:
        global_data.exit_empty_confirmed_at = dt.datetime.now()
        global_data.exit_empty_fast_skip_until_ts = _EMPTY_CONFIRMED_AT_TS + _env_float("EXIT_EMPTY_FAST_SKIP_TTL_SEC", 10.0)
    except Exception:
        pass


def _clear_empty_confirmed() -> None:
    global _EMPTY_CONFIRMED_AT_TS
    _EMPTY_CONFIRMED_AT_TS = None
    try:
        global_data.exit_empty_fast_skip_until_ts = None
    except Exception:
        pass


def install_exit_order_sender_safe() -> bool:
    logger.info("[startup.scheduler_startup] exit order sender install start")
    try:
        from trading.exit.order_sender import install_exit_order_sender
        ok = bool(install_exit_order_sender())
        try:
            global_data.exit_order_sender_installed = ok
            global_data.exit_order_sender_installed_at = dt.datetime.now()
            global_data.exit_order_sender_install_failed = not ok
        except Exception:
            pass
        if ok:
            logger.info("[startup.scheduler_startup] exit order sender installed")
        else:
            logger.warning("[startup.scheduler_startup] exit order sender install returned False")
        return ok
    except Exception as e:
        try:
            global_data.exit_order_sender_installed = False
            global_data.exit_order_sender_install_failed = True
            global_data.exit_order_sender_install_error = str(e)
            global_data.exit_order_sender_installed_at = dt.datetime.now()
        except Exception:
            pass
        logger.exception("[startup.scheduler_startup] exit order sender install failed")
        return False


def _skip_reason() -> str:
    try:
        if _broker_authoritative_empty_hint():
            return "broker_authoritative_empty_explicit_env"
        if _EMPTY_CONFIRMED_AT_TS is not None:
            remain = (_EMPTY_CONFIRMED_AT_TS + _env_float("EXIT_EMPTY_FAST_SKIP_TTL_SEC", 10.0)) - time.time()
            return f"recent_empty_confirmed remain={max(0.0, remain):.3f}s"
    except Exception:
        pass
    return "empty_fast_skip"


def run_exit_loop_market_guarded() -> None:
    global _LAST_STARTED_TS
    try:
        if not is_market_time_for_exit():
            logger.info("[EXIT SCHEDULER] market closed skip")
            return

        _maybe_log_broker_empty_observed()

        if _empty_fast_skip_active():
            logger.info("[EXIT SCHEDULER] empty fast skip reason=%s", _skip_reason())
            _mark_empty_confirmed()
            return

        try:
            from trading.exit.exit_loop import exit_loop_5s
        except Exception:
            logger.exception("[EXIT SCHEDULER] import failed: trading.exit.exit_loop.exit_loop_5s")
            return
        _LAST_STARTED_TS = time.time()
        logger.info("[EXIT SCHEDULER] exit_loop_5s start")
        ret = exit_loop_5s()
        logger.info("[EXIT SCHEDULER] exit_loop_5s done ret=%s", ret)

        if _global_has_open_positions_hint():
            _clear_empty_confirmed()
        else:
            _mark_empty_confirmed()
    except Exception:
        logger.exception("[EXIT SCHEDULER] exit loop failed")


def register_exit_loop_safe() -> bool:
    logger.info("[startup.scheduler_startup] exit loop scheduler bootstrap start")
    try:
        if has_schedule_tag("exit_loop_5s"):
            logger.info("[startup.scheduler_startup] exit loop already registered")
            try:
                global_data.exit_loop_scheduler_registered = True
                global_data.exit_loop_scheduler_failed = False
            except Exception:
                pass
            return True
        schedule.every(5).seconds.do(run_exit_loop_market_guarded).tag("exit_loop_5s", "exit")
        try:
            global_data.exit_loop_scheduler_registered = True
            global_data.exit_loop_scheduler_registered_at = dt.datetime.now()
            global_data.exit_loop_scheduler_failed = False
            global_data.exit_loop_scheduler_error = ""
            global_data.exit_broker_empty_immediate_skip_default = "0"
        except Exception:
            pass
        logger.info("[startup.scheduler_startup] exit loop scheduler registered every=5s broker_empty_immediate_skip_default=0")
        log_scheduler_snapshot("after exit loop scheduler register")
        return True
    except Exception:
        logger.exception("[startup.scheduler_startup] exit loop scheduler register failed")
        try:
            global_data.exit_loop_scheduler_registered = False
            global_data.exit_loop_scheduler_failed = True
        except Exception:
            pass
        return False


def install() -> bool:
    ok1 = install_exit_order_sender_safe()
    ok2 = register_exit_loop_safe()
    return bool(ok1 and ok2)


__all__ = ["install", "run_exit_loop_market_guarded", "register_exit_loop_safe", "install_exit_order_sender_safe"]
