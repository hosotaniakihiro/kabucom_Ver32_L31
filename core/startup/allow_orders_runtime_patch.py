# ============================================================
# File   : core/startup/allow_orders_runtime_patch.py
# Version: PRODUCTION-STABLE-REV1-ALLOW-ORDERS-RUNTIME-PATCH
# ------------------------------------------------------------
# Purpose:
#   - GlobalContext の allow_orders 初期値 False により、
#     AI_OK / pending 登録後に position_filter で全件 ENTRY禁止になる問題を修正する。
#   - 起動時に ALLOW_ORDERS / DRY_RUN 系 env を見て global_context.allow_orders を明示設定する。
#
# Policy:
#   - ALLOW_ORDERS=0 / false / off / no なら必ず停止。
#   - DRY_RUN=1 / ENTRY_DRY_RUN=1 / ORDER_DRY_RUN=1 なら必ず停止。
#   - 未設定時は実運用前提で True にする。
#   - ログに最終状態と理由を必ず出す。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False

_TRUE_VALUES = {"1", "true", "yes", "on", "y", "live", "real"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "dry", "dryrun", "paper"}


def _norm(v: Any) -> str:
    try:
        return str(v).strip().lower()
    except Exception:
        return ""


def _env_bool(name: str, default: bool | None = None) -> bool | None:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    s = _norm(v)
    if s in _TRUE_VALUES:
        return True
    if s in _FALSE_VALUES:
        return False
    return default


def _dry_run_requested() -> tuple[bool, str]:
    for key in (
        "DRY_RUN",
        "ENTRY_DRY_RUN",
        "ORDER_DRY_RUN",
        "KABU_DRY_RUN",
        "SUMMARY_ENTRY_DRY_RUN",
        "PAPER_TRADE",
    ):
        v = _env_bool(key, None)
        if v is True:
            return True, key
    return False, ""


def _resolve_allow_orders() -> tuple[bool, str]:
    dry, dry_key = _dry_run_requested()
    if dry:
        return False, f"{dry_key}=true"

    for key in (
        "ALLOW_ORDERS",
        "ENTRY_ALLOW_ORDERS",
        "KABU_ALLOW_ORDERS",
        "LIVE_ORDERS",
        "ORDER_LIVE",
    ):
        v = _env_bool(key, None)
        if v is True:
            return True, f"{key}=true"
        if v is False:
            return False, f"{key}=false"

    return True, "default_live_true"


def install_allow_orders_runtime_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    try:
        from core.global_context.context import global_context
        from global_state import global_data
    except Exception:
        logger.exception("[ALLOW ORDERS PATCH] import failed")
        return

    allow, reason = _resolve_allow_orders()

    try:
        setattr(global_context, "allow_orders", bool(allow))
    except Exception:
        logger.exception("[ALLOW ORDERS PATCH] set global_context.allow_orders failed")

    try:
        # GlobalDataCompat は __getattr__ 経由で GlobalContext を見るが、
        # 明示属性がある環境にも対応して同じ値を入れる。
        setattr(global_data, "allow_orders", bool(allow))
    except Exception:
        logger.debug("[ALLOW ORDERS PATCH] set global_data.allow_orders skipped", exc_info=True)

    _INSTALLED = True
    logger.warning(
        "[ALLOW ORDERS PATCH] installed allow_orders=%s reason=%s env_ALLOW_ORDERS=%s env_DRY_RUN=%s env_ENTRY_DRY_RUN=%s env_ORDER_DRY_RUN=%s",
        bool(allow),
        reason,
        os.environ.get("ALLOW_ORDERS"),
        os.environ.get("DRY_RUN"),
        os.environ.get("ENTRY_DRY_RUN"),
        os.environ.get("ORDER_DRY_RUN"),
    )


__all__ = ["install_allow_orders_runtime_patch"]
