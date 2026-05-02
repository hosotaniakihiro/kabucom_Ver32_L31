# ============================================================
# File   : trading/push/subscription_manager/core.py
# Version: V3.2-PUSH-SUBSCRIPTION-CORE-THIN-STRICT-50-ROTATION-FORCE-NOSKIP
# ------------------------------------------------------------
# Function:
#   - subscription manager 公開API
#   - target_builder.py で50銘柄以内の登録対象を作成
#   - refresh_policy.py でrefresh可否を判定
#   - register_ops.run_refresh_sequence() を実行
#
# Important:
#   - ranking_selector.py は最大100銘柄候補を作る
#   - core.py に来る target_symbols は target_builder.py により50件以内
#   - 念のため core.py でも最終防衛として50件に制限する
#   - rotation時の unregister_all → 0.5秒待機 → register を
#     register_ops.py へ渡す
#
# Fix:
#   - del kwargs を廃止
#   - wait_after_clear_sec / unregister_wait_sec を run_refresh_sequence へ渡す
#   - clear_first=True の時は毎回全解除してから登録する
#   - rotation_A / rotation_B は skip 判定を無効化
#   - rotation_A / rotation_B は force=True / clear_first=True / unregister_first=True
#   - rotation時は必ず 0.5秒待機を register_ops.py へ渡す
# ============================================================

from __future__ import annotations

import logging
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


DEFAULT_WAIT_AFTER_CLEAR_SEC = 0.5


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_rotation_reason(reason: Any) -> bool:
    """
    push_stream.rotation.py から来る reason かどうか判定する。

    対象例:
      - rotation_A
      - rotation_B
      - push_rotation_A
      - push_rotation_B
    """
    try:
        s = str(reason or "").strip().lower()
        return (
            s.startswith("rotation_")
            or s.startswith("push_rotation_")
            or s in {"rotation", "rotate"}
        )
    except Exception:
        return False


def _extract_wait_after_clear_sec(kwargs: dict[str, Any]) -> float:
    """
    push_stream.rotation から渡される待機秒を取り出す。

    優先:
      1. wait_after_clear_sec
      2. unregister_wait_sec
      3. clear_wait_sec
      4. wait_after_unregister_sec
      5. DEFAULT_WAIT_AFTER_CLEAR_SEC
    """
    for key in (
        "wait_after_clear_sec",
        "unregister_wait_sec",
        "clear_wait_sec",
        "wait_after_unregister_sec",
    ):
        if key in kwargs and kwargs.get(key) is not None:
            return max(
                0.0,
                _safe_float(kwargs.get(key), DEFAULT_WAIT_AFTER_CLEAR_SEC),
            )

    return DEFAULT_WAIT_AFTER_CLEAR_SEC


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

    Rotation時:
      push_stream.rotation から
        reason=rotation_A / rotation_B
        clear_first=True
        unregister_first=True
        wait_after_clear_sec=0.5
      が渡される想定。

      その場合:
        skipせず、
        unregister_all
        → 0.5秒待機
        → register 50銘柄
      を毎回実行する。
    """

    is_rotation = _is_rotation_reason(reason)
    wait_after_clear_sec = _extract_wait_after_clear_sec(kwargs)

    if is_rotation:
        force = True
        clear_first = True
        unregister_first = True

        if wait_after_clear_sec <= 0:
            wait_after_clear_sec = DEFAULT_WAIT_AFTER_CLEAR_SEC

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

    # --------------------------------------------------------
    # rotation_A / rotation_B は必ず全解除→登録したい。
    # そのため通常の skip 判定を通さない。
    # --------------------------------------------------------
    if is_rotation:
        skip = False
        skip_guard = "rotation_force_noskip"
        decided_clear_first = True
        unregister_first = True
        clear_policy = "rotation_force_clear"
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

    if is_rotation:
        logger.info(
            "[SUB MANAGER CORE] rotation force refresh reason=%s force=%s clear_first=%s unregister_first=%s "
            "current=%d target=%d removed=%d added=%d diff_ratio=%.3f wait_after_clear=%.3fs",
            reason,
            force,
            decided_clear_first,
            unregister_first,
            len(current_symbols),
            len(target_symbols),
            len(removed),
            len(added),
            diff_ratio,
            wait_after_clear_sec,
        )
    else:
        logger.info(
            "[SUB MANAGER CORE] refresh start reason=%s force=%s clear_first=%s clear_policy=%s "
            "current=%d target=%d removed=%d added=%d diff_ratio=%.3f wait_after_clear=%.3fs",
            reason,
            force,
            decided_clear_first,
            clear_policy,
            len(current_symbols),
            len(target_symbols),
            len(removed),
            len(added),
            diff_ratio,
            wait_after_clear_sec,
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
            "[SUB MANAGER CORE] refresh done reason=%s current=%d target=%d rotation=%s wait_after_clear=%.3fs",
            reason,
            len(current_symbols),
            len(target_symbols),
            is_rotation,
            wait_after_clear_sec,
        )
        return True

    logger.warning(
        "[SUB MANAGER CORE] refresh failed reason=%s current=%d target=%d removed=%d added=%d diff_ratio=%.3f rotation=%s",
        reason,
        len(current_symbols),
        len(target_symbols),
        len(removed),
        len(added),
        diff_ratio,
        is_rotation,
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