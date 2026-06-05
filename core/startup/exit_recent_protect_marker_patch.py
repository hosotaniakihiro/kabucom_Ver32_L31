# ============================================================
# File   : core/startup/exit_recent_protect_marker_patch.py
# Version: V1-EXIT-COOLDOWN-PROTECT
# ------------------------------------------------------------
# 目的:
#   EXIT完了直後の銘柄を、建玉同期・kabu反映遅延対策で短時間だけ
#   PUSH固定枠に残し、その後は自動で固定枠から外す。
#
# 動き:
#   execute_exit 成功後:
#     global_data.active_protected_exit_cooldown_until[symbol] = now + 60秒
#   protected.py 側:
#     期限内だけ protected に追加し、期限切れは自動削除。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_EXECUTE_EXIT = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _symbol_from_pos(pos: Any) -> str:
    try:
        if isinstance(pos, dict):
            s = str(pos.get("symbol") or pos.get("Symbol") or pos.get("stock_code") or "").strip()
        else:
            s = str(getattr(pos, "symbol", None) or getattr(pos, "Symbol", None) or getattr(pos, "stock_code", None) or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _mark_exit_cooldown(symbol: str) -> None:
    if not symbol or not _env_bool("ACTIVE_PROTECT_EXIT_COOLDOWN_SYMBOLS", True):
        return
    sec = max(0.0, _env_float("ACTIVE_EXIT_COOLDOWN_PROTECT_SEC", 60.0))
    if sec <= 0:
        return
    try:
        from global_state import global_data
        mp = getattr(global_data, "active_protected_exit_cooldown_until", None)
        if not isinstance(mp, dict):
            mp = {}
            setattr(global_data, "active_protected_exit_cooldown_until", mp)
        until = dt.datetime.now() + dt.timedelta(seconds=sec)
        mp[str(symbol)] = until
        logger.warning("[EXIT RECENT PROTECT MARKER] mark symbol=%s until=%s sec=%.1f", symbol, until.isoformat(timespec="seconds"), sec)
    except Exception:
        logger.exception("[EXIT RECENT PROTECT MARKER] mark failed symbol=%s", symbol)


def install() -> bool:
    global _INSTALLED, _ORIG_EXECUTE_EXIT
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("ACTIVE_PROTECT_EXIT_COOLDOWN_SYMBOLS", "1")
        os.environ.setdefault("ACTIVE_EXIT_COOLDOWN_PROTECT_SEC", "60")
        import trading.handlers.exit_handler as eh
        cur = getattr(eh, "execute_exit", None)
        if getattr(cur, "_exit_recent_protect_marker_v1", False):
            _INSTALLED = True
            return True
        if not callable(cur):
            logger.warning("[EXIT RECENT PROTECT MARKER] execute_exit target missing")
            return False
        _ORIG_EXECUTE_EXIT = getattr(cur, "_original", cur)

        @wraps(_ORIG_EXECUTE_EXIT)
        def wrapped_execute_exit(pos, exit_price, reason):
            symbol = _symbol_from_pos(pos)
            ok = _ORIG_EXECUTE_EXIT(pos, exit_price, reason)
            try:
                if ok:
                    _mark_exit_cooldown(symbol)
            except Exception:
                logger.exception("[EXIT RECENT PROTECT MARKER] post execute failed symbol=%s", symbol)
            return ok

        wrapped_execute_exit._exit_recent_protect_marker_v1 = True  # type: ignore[attr-defined]
        wrapped_execute_exit._original = _ORIG_EXECUTE_EXIT  # type: ignore[attr-defined]
        eh.execute_exit = wrapped_execute_exit
        _INSTALLED = True
        logger.warning(
            "[EXIT RECENT PROTECT MARKER] installed v1 enabled=%s sec=%s",
            os.environ.get("ACTIVE_PROTECT_EXIT_COOLDOWN_SYMBOLS"),
            os.environ.get("ACTIVE_EXIT_COOLDOWN_PROTECT_SEC"),
        )
        return True
    except Exception:
        logger.exception("[EXIT RECENT PROTECT MARKER] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[EXIT RECENT PROTECT MARKER] auto install failed")

__all__ = ["install"]
