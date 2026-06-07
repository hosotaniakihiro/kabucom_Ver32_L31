# ============================================================
# File   : core/startup/board_runtime_self_check_patch.py
# Version: V1.1-BOARD-RUNTIME-SELF-CHECK-PERSIST
# ------------------------------------------------------------
# マーケット時間外でも確認できる起動時セルフチェック。
# APIは叩かず、runtime patchが実際にwrap/起動されているかを確認する。
#
# V1.1:
#   - runtime/diagnostics/board_runtime_self_check.json に結果保存
#   - ログが流れても後から確認できるようにする
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _b(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _has_attr(obj: Any, attr: str) -> bool:
    try:
        return bool(getattr(obj, attr, False))
    except Exception:
        return False


def _chain_has(fn: Any, attr: str, limit: int = 30) -> bool:
    seen: set[int] = set()
    cur = fn
    for _ in range(limit):
        if not callable(cur):
            return False
        ident = id(cur)
        if ident in seen:
            return False
        seen.add(ident)
        if _has_attr(cur, attr):
            return True
        cur = getattr(cur, "_original", None) or getattr(cur, "_rest_full_board_original", None)
    return False


def _check_entry_order_builder() -> dict:
    out = {"available": False, "rest_full_board_wrapped": False}
    try:
        import trading.handlers.entry_order_builder as eob
        fn = getattr(eob, "build_entry_order", None)
        out["available"] = callable(fn)
        out["rest_full_board_wrapped"] = _chain_has(fn, "_rest_full_board_patch") if callable(fn) else False
    except Exception as e:
        out["error"] = repr(e)
    return out


def _check_exit_executor() -> dict:
    out = {
        "available": False,
        "limit_board_touch_wrapped": False,
        "pending_close_wrapped": False,
    }
    try:
        import trading.exit.executor as ex
        close_payload = getattr(ex, "_build_kabu_close_payload", None)
        close_db = getattr(ex, "_close_db_position", None)
        out["available"] = callable(close_payload) or callable(close_db)
        if callable(close_payload):
            out["limit_board_touch_wrapped"] = (
                _chain_has(close_payload, "_exit_limit_board_touch_wrapped_v11")
                or _chain_has(close_payload, "_exit_limit_board_touch_wrapped")
            )
        out["pending_close_wrapped"] = _chain_has(close_db, "_exit_limit_pending_close_wrapped") if callable(close_db) else False
    except Exception as e:
        out["error"] = repr(e)
    return out


def _check_global_data_retry_hook() -> dict:
    out = {"add_entry_inflight_wrapped": False}
    try:
        from global_state import global_data
        fn = getattr(global_data, "add_entry_inflight", None)
        out["add_entry_inflight_wrapped"] = _chain_has(fn, "_summary_retry_add_wrapped") if callable(fn) else False
    except Exception as e:
        out["error"] = repr(e)
    return out


def _check_urlopen_monitor() -> dict:
    fn = urllib.request.urlopen
    return {
        "urlopen_wrapped": _has_attr(fn, "_board_rest_api_monitor_wrapped"),
        "monitor_enabled_env": _b("BOARD_REST_API_MONITOR_ENABLED"),
    }


def _diagnostics_path() -> Path:
    raw = os.getenv("BOARD_RUNTIME_SELF_CHECK_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path("runtime") / "diagnostics" / "board_runtime_self_check.json"


def _save_result(payload: dict) -> None:
    try:
        p = _diagnostics_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        logger.warning("[BOARD RUNTIME SELF CHECK] saved path=%s", p)
    except Exception:
        logger.exception("[BOARD RUNTIME SELF CHECK] save diagnostics failed")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    checks = {
        "entry_order_builder": _check_entry_order_builder(),
        "exit_executor": _check_exit_executor(),
        "summary_retry": _check_global_data_retry_hook(),
        "urlopen_monitor": _check_urlopen_monitor(),
        "env": {
            "entry_rest": _b("ENTRY_REST_FULL_BOARD_ENABLED"),
            "entry_double_check": _b("ENTRY_REST_FULL_BOARD_DOUBLE_CHECK_ENABLED"),
            "exit_rest": _b("EXIT_REST_FULL_BOARD_ENABLED"),
            "exit_reprice": _b("EXIT_UNFILLED_REPRICE_ENABLED"),
            "exit_fill_confirm": _b("EXIT_FILL_CONFIRM_ENABLED"),
            "exit_closing_reconcile": _b("EXIT_CLOSING_RECONCILE_ENABLED"),
            "api_monitor": _b("BOARD_REST_API_MONITOR_ENABLED"),
        },
    }
    ok = True
    try:
        ok = bool(checks["entry_order_builder"].get("rest_full_board_wrapped"))
        ok = ok and bool(checks["exit_executor"].get("limit_board_touch_wrapped"))
        ok = ok and bool(checks["exit_executor"].get("pending_close_wrapped"))
        ok = ok and bool(checks["urlopen_monitor"].get("urlopen_wrapped"))
    except Exception:
        ok = False

    payload = {
        "ok": bool(ok),
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
        "diagnostics_path": str(_diagnostics_path()),
    }
    _save_result(payload)
    level = logger.warning if ok else logger.error
    level("[BOARD RUNTIME SELF CHECK] ok=%s checks=%s", ok, checks)
    _INSTALLED = True
    return ok


try:
    install()
except Exception:
    logger.exception("[BOARD RUNTIME SELF CHECK] auto install failed")


__all__ = ["install"]
