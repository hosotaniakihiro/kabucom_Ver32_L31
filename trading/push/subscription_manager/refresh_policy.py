# ============================================================
# File   : trading/push/subscription_manager/refresh_policy.py
# Version: V1.0-PUSH-SUBSCRIPTION-REFRESH-POLICY
# ------------------------------------------------------------
# Purpose:
#   - on_open storm guard
#   - stale override
#   - clear_first / unregister_first policy
#   - refresh change stats
# ============================================================

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from . import state
from .globals_access import safe_get_global_data, safe_getattr
from .guards import is_on_open_reason, is_push_stale, reason_key

logger = logging.getLogger(__name__)

VENDOR_SAFE_DISABLE_UNSUBSCRIBE_DEFAULT = False
AUTO_CLEAR_ON_TARGET_CHANGE_DEFAULT = True
AUTO_CLEAR_ON_FIRST_RUN_DEFAULT = True

ON_OPEN_MIN_REFRESH_GAP_SEC = 4.0
ON_OPEN_FORCE_SKIP_IF_UNCHANGED = True


def now_ts() -> float:
    return time.time()


def safe_bool_like(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "t", "yes", "y", "on"):
            return True
        if s in ("0", "false", "f", "no", "n", "off", ""):
            return False
    return default


def reason_elapsed(reason: str) -> float:
    key = reason_key(reason)
    with state.manager_lock:
        ts = state.last_refresh_reason_ts.get(key, 0.0)
    if ts <= 0:
        return 10**9
    return max(0.0, now_ts() - ts)


def mark_reason(reason: str) -> None:
    key = reason_key(reason)
    with state.manager_lock:
        state.last_refresh_reason_ts[key] = now_ts()


def target_fingerprint(symbols: Sequence[str]) -> str:
    try:
        return "|".join(symbols) if symbols else ""
    except Exception:
        return ""


def get_vendor_safe_disable_unsubscribe() -> bool:
    gd = safe_get_global_data()
    if gd is not None:
        for name in (
            "push_vendor_safe_disable_unsubscribe",
            "vendor_safe_disable_unsubscribe",
            "subscription_vendor_safe_disable_unsubscribe",
        ):
            v = safe_getattr(gd, name, None)
            if v is not None:
                return safe_bool_like(v, VENDOR_SAFE_DISABLE_UNSUBSCRIBE_DEFAULT)
    return VENDOR_SAFE_DISABLE_UNSUBSCRIBE_DEFAULT


def get_auto_clear_on_target_change() -> bool:
    gd = safe_get_global_data()
    if gd is not None:
        for name in (
            "push_auto_clear_on_target_change",
            "subscription_auto_clear_on_target_change",
            "auto_clear_on_target_change",
        ):
            v = safe_getattr(gd, name, None)
            if v is not None:
                return safe_bool_like(v, AUTO_CLEAR_ON_TARGET_CHANGE_DEFAULT)
    return AUTO_CLEAR_ON_TARGET_CHANGE_DEFAULT


def get_auto_clear_on_first_run() -> bool:
    gd = safe_get_global_data()
    if gd is not None:
        for name in (
            "push_auto_clear_on_first_run",
            "subscription_auto_clear_on_first_run",
            "auto_clear_on_first_run",
        ):
            v = safe_getattr(gd, name, None)
            if v is not None:
                return safe_bool_like(v, AUTO_CLEAR_ON_FIRST_RUN_DEFAULT)
    return AUTO_CLEAR_ON_FIRST_RUN_DEFAULT


def refresh_change_stats(current: Sequence[str], target: Sequence[str]) -> dict:
    cur = list(current or [])
    tgt = list(target or [])

    cur_set = set(cur)
    tgt_set = set(tgt)

    removed = [s for s in cur if s not in tgt_set]
    added = [s for s in tgt if s not in cur_set]

    base = max(len(cur), len(tgt), 1)
    ratio = (len(removed) + len(added)) / float(base)

    return {
        "removed": removed,
        "added": added,
        "ratio": ratio,
    }


def decide_clear_first(
    *,
    current: Sequence[str],
    target: Sequence[str],
    requested_clear: bool,
    vendor_safe_disable_unsubscribe: bool,
    auto_clear_on_target_change: bool,
) -> tuple[bool, str]:
    if vendor_safe_disable_unsubscribe:
        return False, "vendor_safe_disable_unsubscribe"

    if requested_clear:
        return True, "requested"

    if not current and target and get_auto_clear_on_first_run():
        return True, "first_run"

    if auto_clear_on_target_change and list(current) != list(target):
        return True, "target_changed"

    return False, "not_needed"


def should_skip_on_open_refresh(
    reason: str,
    force: bool,
    current: Sequence[str],
    target: Sequence[str],
    *,
    removed_count: int = 0,
    added_count: int = 0,
    diff_ratio: float = 0.0,
) -> tuple[bool, str]:
    if not is_on_open_reason(reason):
        return False, ""

    elapsed = reason_elapsed(reason)
    current_fp = target_fingerprint(current)
    target_fp = target_fingerprint(target)

    if elapsed < ON_OPEN_MIN_REFRESH_GAP_SEC:
        return True, f"storm_guard elapsed={elapsed:.2f}s"

    if is_push_stale(reason=reason):
        return False, "push_stale_override"

    unchanged = (
        current_fp
        and current_fp == target_fp
        and int(removed_count or 0) == 0
        and int(added_count or 0) == 0
        and float(diff_ratio or 0.0) <= 0.0
    )

    if ON_OPEN_FORCE_SKIP_IF_UNCHANGED and unchanged:
        if force:
            return True, "force_on_open_but_unchanged"
        return True, "on_open_unchanged"

    return False, ""


__all__ = [
    "now_ts",
    "mark_reason",
    "target_fingerprint",
    "get_vendor_safe_disable_unsubscribe",
    "get_auto_clear_on_target_change",
    "get_auto_clear_on_first_run",
    "refresh_change_stats",
    "decide_clear_first",
    "should_skip_on_open_refresh",
]
