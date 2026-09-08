# ============================================================
# File   : trading/push/push_stream/start_trace.py
# Version: V1.0-PUSH-START-TRACE
# ------------------------------------------------------------
# PUSH 受信が「起動したか」を 1 種類のログで追えるようにする。
#
# 背景 (2026-09-03 実測):
#   push_receiver_runner.py (pid=41016) は heartbeat を出し続け
#   push_started=True を報告していたが、
#   WebSocket も writer も flush worker も一度も起動していなかった。
#
#     owner=True role=push_receiver uptime=1186s
#     active_ws=0 connected=False generation=0 ws_instance_id=none
#     ws_thread_alive=None flush_thread_alive=None writer_ready=False
#
#   原因は runner.start_push_stream() が ws_owner ロックを取れずに
#   **戻り値 None で return** し、呼び出し側 (data_collectors.push_runtime)
#   が「例外が出なかった = 起動成功」と解釈していたこと。
#   起動経路のどこで止まったのかがログから読めなかった。
#
# 方針:
#   - 起動経路の各段階に必ず 1 行出す (silent return 禁止)。
#   - 途中で止まるときは必ず ABORT 行に理由を書く。
#   - 1 行に pid / thread / owner / role / generation / symbol_count /
#     token_tail / 起動可否に効く env を必ず載せる。
#
# ステップ番号 (呼び出し順):
#   01 runtime_enter     data_collectors/push_runtime.run_forever
#   02 owner_resolved    ownership.is_push_owner + ws_owner ロック取得
#   03 symbols_ready     PUSH 登録対象 100 銘柄の seed
#   04 writer_start      state._stream_writer 注入
#   05 flush_start       flush worker スレッド
#   06 ws_runner_create  WebSocketApp 生成
#   07 ws_thread_start   push-ws-thread の start
#   08 run_forever_enter ws_app.run_forever() 直前
#   09 on_open           WebSocket 接続確立
#   10 register_start    kabu Station への銘柄登録開始
#   11 register_done     登録完了
#   12 first_message     最初の PUSH 受信
# ============================================================

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger("trading.push.push_stream.start_trace")

VERSION = "V1.0-PUSH-START-TRACE"

# 起動可否に効く env。値が無いものは "-" で出す (未設定も情報)。
_TRACE_ENV_KEYS = (
    "PUSH_STREAM_FORCE_OWNER",
    "PUSH_STREAM_OWNER_ROLE",
    "PUSH_WS_OWNER_ROLE",
    "KABU_PUSH_WS_OWNER_LOCK",
    "AUTOSTOCK_MAIN_PUSH_WS_ENABLED",
    "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
    "AUTOSTOCK_MAIN_MEMORY_ONLY",
    "AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN",
    "PUSH_STREAM_DB_WRITE",
    "PUSH_DB_WRITE_ENABLED",
    "PUSH_STREAM_ENABLE_DB_WRITE",
    "PUSH_STREAM_ORDER_BOOK_WRITE",
    "PUSH_ROTATION_ENABLED",
    "PUSH_STREAM_ENABLE_ROTATION",
    "PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH",
)

_STEP_NAMES = {
    1: "runtime_enter",
    2: "owner_resolved",
    3: "symbols_ready",
    4: "writer_start",
    5: "flush_start",
    6: "ws_runner_create",
    7: "ws_thread_start",
    8: "run_forever_enter",
    9: "on_open",
    10: "register_start",
    11: "register_done",
    12: "first_message",
}

_once_lock = threading.Lock()
_once_seen: set[str] = set()


def _env_text() -> str:
    out = []
    for key in _TRACE_ENV_KEYS:
        try:
            v = os.environ.get(key)
        except Exception:
            v = None
        out.append("%s=%s" % (key, "-" if v is None else str(v).strip()))
    return " ".join(out)


def _owner_fields() -> tuple[Any, Any, Any, Any]:
    """(owner, role, reason, generation)。取れない項目は None。"""
    owner = role = reason = generation = None
    try:
        from .ownership import is_push_owner

        owner, role, reason = is_push_owner()
    except Exception:
        pass
    try:
        from .ws_callbacks import current_connection_generation

        generation = current_connection_generation()
    except Exception:
        pass
    return owner, role, reason, generation


def _symbol_count() -> Any:
    """PUSH 登録対象として seed 済みの銘柄数。"""
    try:
        from global_state import global_data
    except Exception:
        return None
    for attr in ("push_symbol_seed_count", "symbols_active", "monitor_symbols", "push_symbols"):
        try:
            v = getattr(global_data, attr, None)
        except Exception:
            continue
        if v is None:
            continue
        if isinstance(v, int):
            return v
        try:
            return len(v)
        except Exception:
            continue
    return None


def _token_tail() -> Any:
    try:
        tok = str(os.getenv("API_TOKEN") or "")
        return tok[-4:] if tok else None
    except Exception:
        return None


def _ws_owner_lock_state() -> str:
    try:
        from .ws_owner import is_push_ws_owner, active_ws_instance_count

        return "lock_held=%s active_ws=%s" % (is_push_ws_owner(), active_ws_instance_count())
    except Exception:
        return "lock_held=? active_ws=?"


def _common_fields() -> str:
    owner, role, reason, generation = _owner_fields()
    try:
        thread_id = threading.get_ident()
        thread_name = threading.current_thread().name
    except Exception:
        thread_id, thread_name = -1, "?"
    return (
        "pid=%s ppid=%s thread_id=%s thread=%s owner=%s role=%s owner_reason=%s "
        "generation=%s symbol_count=%s token_tail=%s %s %s"
        % (
            os.getpid(),
            _safe_ppid(),
            thread_id,
            thread_name,
            owner,
            role,
            reason,
            generation,
            _symbol_count(),
            _token_tail(),
            _ws_owner_lock_state(),
            _env_text(),
        )
    )


def _safe_ppid() -> int:
    try:
        return int(os.getppid())
    except Exception:
        return -1


def _fmt_extra(extra: dict) -> str:
    if not extra:
        return ""
    parts = []
    for k, v in extra.items():
        parts.append("%s=%s" % (k, v))
    return " " + " ".join(parts)


def trace(step_no: int, step: str | None = None, /, **extra: Any) -> None:
    """通常の通過ログ。例外は絶対に投げない。"""
    try:
        name = step or _STEP_NAMES.get(int(step_no), "step%02d" % int(step_no))
        logger.warning(
            "[PUSH START TRACE] %02d %s %s%s",
            int(step_no),
            name,
            _common_fields(),
            _fmt_extra(extra),
        )
    except Exception:
        try:
            logger.warning("[PUSH START TRACE] %s (fields unavailable)", step_no)
        except Exception:
            pass


def trace_once(key: str, step_no: int, step: str | None = None, /, **extra: Any) -> None:
    """同じ key では 1 回だけ出す (on_message / register 用)。"""
    try:
        with _once_lock:
            if key in _once_seen:
                return
            _once_seen.add(key)
    except Exception:
        pass
    trace(step_no, step, **extra)


def reset_once(prefix: str = "") -> None:
    """接続世代が変わったときに first_message を再度出せるようにする。"""
    try:
        with _once_lock:
            if not prefix:
                _once_seen.clear()
                return
            for k in [k for k in _once_seen if k.startswith(prefix)]:
                _once_seen.discard(k)
    except Exception:
        pass


def abort(step_no: int, reason: str, step: str | None = None, /, **extra: Any) -> None:
    """途中で return / skip するときは必ずこれを出す。silent return 禁止。"""
    try:
        name = step or _STEP_NAMES.get(int(step_no), "step%02d" % int(step_no))
        logger.error(
            "[PUSH START TRACE] ABORT step=%02d %s reason=%s %s%s",
            int(step_no),
            name,
            reason,
            _common_fields(),
            _fmt_extra(extra),
        )
    except Exception:
        try:
            logger.error("[PUSH START TRACE] ABORT step=%s reason=%s", step_no, reason)
        except Exception:
            pass


__all__ = ["VERSION", "trace", "trace_once", "abort", "reset_once"]
