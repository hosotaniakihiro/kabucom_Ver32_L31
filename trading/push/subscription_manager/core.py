# ============================================================
# File   : trading/push/subscription_manager/core.py
# Version: V3.7-SAFE-RECONNECT-NO-EMPTY-CLEAR
# ------------------------------------------------------------
# Function:
#   - subscription manager 公開API
#   - target_builder.py で50銘柄以内の登録対象を作成
#   - refresh_policy.py でrefresh可否を判定
#   - register_ops.run_refresh_sequence() を実行
#
# Important:
#   - kabu Station の同時登録上限は50銘柄
#   - PUSHローテーションの設計は以下:
#       登録 -> 4.8秒保持 -> 解除 -> 0.2秒待機 -> 登録 -> 4.8秒保持
#   - startup等の初回clearは安全側に1.5秒待つが、rotation_* 理由では
#     PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2 を優先する。
#   - on_open / reconnect 系は target=0 で既存登録を消さない。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from . import state
from .manager_loop import (
    start_symbol_subscription_manager,
    stop_symbol_subscription_manager,
)
from .refresh_policy import (
    decide_clear_first,
    get_auto_clear_on_target_change,
    get_vendor_safe_disable_unsubscribe,
    mark_reason,
    now_ts,
    refresh_change_stats,
    should_skip_on_open_refresh,
    target_fingerprint,
)
from .register_ops import run_refresh_sequence
from .register_symbol_logger import log_kabustation_register_symbols
from .rotation import REGISTER_CHUNK_SIZE, enforce_register_limit
from .target_builder import REGISTER_MAX_SYMBOLS, build_target_symbols

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _default_wait_after_clear_sec() -> float:
    # startup用の安全側既定値。rotation_* では使わない。
    return max(0.0, _env_float("KABU_REGISTER_UNREGISTER_WAIT_SEC", 1.5))


def _rotation_wait_after_clear_sec() -> float:
    # ユーザー設計: 登録 -> 4.8秒 -> 解除 -> 0.2秒 -> 登録。
    return max(0.0, _env_float("PUSH_ROTATION_UNREGISTER_WAIT_SEC", 0.2))


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_rotation_reason(reason: Any) -> bool:
    try:
        s = str(reason or "").strip().lower()
        return (
            s.startswith("rotation_")
            or s.startswith("push_rotation_")
            or s in {"rotation", "rotate"}
        )
    except Exception:
        return False


def _is_safe_reconnect_reason(reason: Any) -> bool:
    """
    WebSocket再接続直後の安全refresh。
    ranking DB が空の瞬間でも既存PUSH登録を0件で上書きしない。
    """
    try:
        s = str(reason or "").strip().lower()
        if not s:
            return False
        return (
            s == "on_open"
            or s.startswith("on_open")
            or s.startswith("reconnect")
            or s.startswith("ws_reconnect")
        )
    except Exception:
        return False


def _is_force_clear_reason(reason: Any) -> bool:
    """
    kabu Station に既存登録が残っている可能性が高く、明示的に全解除したいrefresh理由。

    注意:
      - on_open / reconnect はここに含めない。
        再接続直後に ranking DB が空扱いになると target=0 で unregister_all されるため。
      - 必要な場合だけ PUSH_ONOPEN_FORCE_CLEAR=1 で旧挙動に戻せる。
    """
    try:
        s = str(reason or "").strip().lower()
        if not s:
            return False
        if _is_safe_reconnect_reason(s):
            return _env_bool("PUSH_ONOPEN_FORCE_CLEAR", False)
        return (
            s in {"startup", "startup_bridge", "startup_bridge_rotation_a", "startup_bridge_rotation_b"}
            or s.startswith("startup")
        )
    except Exception:
        return False


def _extract_wait_after_clear_sec(kwargs: dict[str, Any], *, is_rotation: bool = False) -> float:
    rotation_default = _rotation_wait_after_clear_sec()
    default_wait = rotation_default if is_rotation else _default_wait_after_clear_sec()
    for key in (
        "wait_after_clear_sec",
        "unregister_wait_sec",
        "clear_wait_sec",
        "wait_after_unregister_sec",
        "wait_after_clear",
    ):
        if key in kwargs and kwargs.get(key) is not None:
            v = max(0.0, _safe_float(kwargs.get(key), default_wait))
            # rotationでは0指定も「0.2秒設計」へ戻す。startupは1.5秒へ戻す。
            return default_wait if v <= 0 else v
    return default_wait


def _normalize_bool_optional(v: Any, default: bool) -> bool:
    try:
        if v is None:
            return bool(default)
        if isinstance(v, bool):
            return bool(v)
        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
        return bool(default)
    except Exception:
        return bool(default)


def refresh_subscriptions(
    symbols: Any = None,
    *,
    reason: str = "manual",
    force: bool = False,
    max_symbols: int = REGISTER_MAX_SYMBOLS,
    clear_first: Optional[bool] = None,
    unregister_first: Optional[bool] = None,
    **kwargs,
) -> bool:
    """
    kabu Station PUSH登録対象を更新する。

    保証:
      - run_refresh_sequence() へ渡す target_symbols は最大50件
      - rotation_* は登録→保持→解除→0.2秒→登録の設計に合わせる
      - on_open / reconnect は0件候補で既存登録を全解除しない
      - startup は既存登録を全解除してから安全側に登録する
    """

    is_rotation = _is_rotation_reason(reason)
    is_safe_reconnect = _is_safe_reconnect_reason(reason)
    is_force_clear = _is_force_clear_reason(reason)
    wait_after_clear_sec = _extract_wait_after_clear_sec(kwargs, is_rotation=is_rotation)

    if is_rotation or is_force_clear:
        force = True
        clear_first = True
        unregister_first = True
        if wait_after_clear_sec <= 0:
            wait_after_clear_sec = _rotation_wait_after_clear_sec() if is_rotation else _default_wait_after_clear_sec()
    elif is_safe_reconnect:
        # 再接続直後はHTTP registerだけを再送し、unregister_allで空白時間を作らない。
        force = True
        clear_first = False
        unregister_first = False
        wait_after_clear_sec = 0.0

    target_symbols = build_target_symbols(
        symbols=symbols,
        max_symbols=max_symbols,
        reason=reason,
    )

    target_symbols = enforce_register_limit(
        target_symbols,
        register_chunk_size=REGISTER_CHUNK_SIZE,
        reason=reason,
    )

    with state.manager_lock:
        current_symbols = list(state.last_registered_symbols)

    if len(target_symbols) == 0 and (is_rotation or is_safe_reconnect or is_force_clear):
        logger.warning(
            "[SUB MANAGER CORE] skip empty safe refresh keep current reason=%s current=%d target=0 rotation=%s reconnect=%s force_clear=%s",
            reason,
            len(current_symbols),
            is_rotation,
            is_safe_reconnect,
            is_force_clear,
        )
        return True

    vendor_safe_disable_unsubscribe = get_vendor_safe_disable_unsubscribe()
    auto_clear_on_target_change = get_auto_clear_on_target_change()

    stats = refresh_change_stats(current_symbols, target_symbols)
    removed = stats["removed"]
    added = stats["added"]
    diff_ratio = stats["ratio"]

    requested_clear = bool(clear_first) if clear_first is not None else bool(unregister_first)

    decided_clear_first, clear_policy = decide_clear_first(
        current=current_symbols,
        target=target_symbols,
        requested_clear=requested_clear,
        vendor_safe_disable_unsubscribe=vendor_safe_disable_unsubscribe,
        auto_clear_on_target_change=auto_clear_on_target_change,
    )

    if is_rotation:
        skip = False
        skip_guard = "rotation_force_noskip"
        decided_clear_first = True
        unregister_first = True
        clear_policy = "rotation_force_clear_0p2"
        wait_after_clear_sec = _rotation_wait_after_clear_sec()
    elif is_force_clear:
        skip = False
        skip_guard = "startup_force_clear"
        decided_clear_first = True
        unregister_first = True
        clear_policy = "startup_force_clear"
    elif is_safe_reconnect:
        skip = False
        skip_guard = "safe_reconnect_register_only"
        decided_clear_first = False
        unregister_first = False
        clear_policy = "safe_reconnect_register_only"
        wait_after_clear_sec = 0.0
    else:
        skip, skip_guard = should_skip_on_open_refresh(
            reason=reason,
            force=force,
            current=current_symbols,
            target=target_symbols,
            removed_count=len(removed),
            added_count=len(added),
            diff_ratio=float(diff_ratio),
        )

    if skip:
        logger.info(
            "[SUB MANAGER CORE] skip refresh reason=%s guard=%s current=%d target=%d removed=%d added=%d diff_ratio=%.3f",
            reason,
            skip_guard,
            len(current_symbols),
            len(target_symbols),
            len(removed),
            len(added),
            diff_ratio,
        )
        return True

    if skip_guard == "push_stale_override":
        force = True
        decided_clear_first = not vendor_safe_disable_unsubscribe
        unregister_first = decided_clear_first
        clear_policy = "push_stale_override"
        logger.warning(
            "[SUB MANAGER CORE] stale override -> force refresh reason=%s current=%d target=%d removed=%d added=%d diff_ratio=%.3f",
            reason,
            len(current_symbols),
            len(target_symbols),
            len(removed),
            len(added),
            diff_ratio,
        )

    logger.info(
        "[SUB MANAGER CORE] refresh start reason=%s force=%s clear_first=%s unregister_first=%s clear_policy=%s "
        "current=%d target=%d removed=%d added=%d diff_ratio=%.3f wait_after_clear=%.3fs rotation=%s reconnect=%s force_clear=%s",
        reason,
        force,
        decided_clear_first,
        unregister_first,
        clear_policy,
        len(current_symbols),
        len(target_symbols),
        len(removed),
        len(added),
        diff_ratio,
        wait_after_clear_sec,
        is_rotation,
        is_safe_reconnect,
        is_force_clear,
    )

    if len(target_symbols) > REGISTER_CHUNK_SIZE:
        logger.error(
            "[SUB MANAGER CORE] BUG: target_symbols still exceeded limit. trim %d -> %d",
            len(target_symbols),
            REGISTER_CHUNK_SIZE,
        )
        target_symbols = target_symbols[:REGISTER_CHUNK_SIZE]

    log_kabustation_register_symbols(
        target_symbols,
        current_symbols=current_symbols,
        added_symbols=added,
        removed_symbols=removed,
        reason=reason,
    )

    ok = run_refresh_sequence(
        current_symbols=current_symbols,
        target_symbols=target_symbols,
        clear_first=bool(decided_clear_first),
        unregister_first=_normalize_bool_optional(unregister_first, bool(decided_clear_first)),
        wait_after_clear_sec=wait_after_clear_sec,
        unregister_wait_sec=wait_after_clear_sec,
    )

    if ok:
        with state.manager_lock:
            state.last_registered_symbols = list(target_symbols)
            state.last_refresh_ts = now_ts()
            state.last_refresh_target_fingerprint = target_fingerprint(target_symbols)

        mark_reason(reason)

        logger.info(
            "[SUB MANAGER CORE] refresh done reason=%s current=%d target=%d rotation=%s reconnect=%s force_clear=%s wait_after_clear=%.3fs",
            reason,
            len(current_symbols),
            len(target_symbols),
            is_rotation,
            is_safe_reconnect,
            is_force_clear,
            wait_after_clear_sec,
        )
        return True

    logger.warning(
        "[SUB MANAGER CORE] refresh failed reason=%s current=%d target=%d removed=%d added=%d diff_ratio=%.3f rotation=%s reconnect=%s force_clear=%s",
        reason,
        len(current_symbols),
        len(target_symbols),
        len(removed),
        len(added),
        diff_ratio,
        is_rotation,
        is_safe_reconnect,
        is_force_clear,
    )
    return False


def force_refresh_subscriptions(
    symbols: Any = None,
    *,
    reason: str = "force_refresh",
    max_symbols: int = REGISTER_MAX_SYMBOLS,
    clear_first: Optional[bool] = True,
    unregister_first: Optional[bool] = True,
    **kwargs,
) -> bool:
    return refresh_subscriptions(
        symbols=symbols,
        reason=reason,
        force=True,
        max_symbols=max_symbols,
        clear_first=clear_first,
        unregister_first=unregister_first,
        **kwargs,
    )


__all__ = [
    "refresh_subscriptions",
    "force_refresh_subscriptions",
    "start_symbol_subscription_manager",
    "stop_symbol_subscription_manager",
]
