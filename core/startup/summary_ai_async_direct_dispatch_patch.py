# ============================================================
# File   : core/startup/summary_ai_async_direct_dispatch_patch.py
# Version: V12-EXECUTOR-DISPATCH-INLINED
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が AI_OK / approved を出しても、実発注が
#   queued_async / snapshot_no_order / entry_controller_no_order で止まる問題を止める。
#
# V12:
#   - execute_ai_ok_entries_bulk 差し替え (_patched_execute_ai_ok_entries_bulk /
#     _fallback_direct_dispatch のrolling snapshot retry) は
#     trading/entry/summary_ai/executor.py 本体 (REV11) へインライン化したため撤去。
#     _positive_result / _entry_price_bounds への差し替えは維持する
#     (別の対象関数として残置)。
#
# V11:
#   - V10 の strict executed 判定、2,500円 price floor、direct snapshot timeout を維持。
#   - direct snapshot が no-order の場合、同じ approved_rows だけを再試行せず、
#     元の ai_results から未試行の AI_OK 候補を追加で approved_row 化して順番に試す。
#   - 低出来高・低変動・blowoff・板ガードは緩めない。各候補は従来の entry_pipeline / final guard を通す。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V12-EXECUTOR-DISPATCH-INLINED"
_INSTALLED = False
_WATCHER_STARTED = False
_POSITIVE_RESULT_PATCHED = False
_PRICE_FLOOR_PATCHED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _summary_ai_price_floor() -> float:
    return max(0.0, _env_float("SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE", 2500.0))


def _strict_result_executed(result: Any) -> bool:
    """True only when an order/entry was actually submitted/executed. approvedだけではTrueにしない。"""
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return bool(result)
        if isinstance(result, dict):
            if result.get("executed") is False:
                return False
            for key in ("executed", "order_sent", "order_submitted", "success", "entry_executed"):
                if bool(result.get(key)):
                    return True
            for key in ("executed_count", "order_count", "submitted_count", "sent_count", "entries"):
                if _safe_int(result.get(key), 0) > 0:
                    return True
            for key in ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols"):
                v = result.get(key)
                if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                    return True
                if v and not isinstance(v, (list, tuple, set, dict)):
                    return True
            for key in ("result", "pipeline_result", "direct_dispatch_result"):
                child = result.get(key)
                if child is not result and _strict_result_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_strict_result_executed(x) for x in result)
        return False
    except Exception:
        return False


# execute_ai_ok_entries_bulk の direct-dispatch rolling snapshot fallback
# (旧 _patched_execute_ai_ok_entries_bulk / _fallback_direct_dispatch とその依存関数) は
# trading/entry/summary_ai/executor.py 本体 (REV11) へインライン化済み。
# _strict_result_executed は _positive_result 差し替え (下記) に引き続き使う。


def _install_executor_positive_result_patch(exec_mod: Any) -> bool:
    global _POSITIVE_RESULT_PATCHED
    try:
        cur = getattr(exec_mod, "_positive_result", None)
        if getattr(cur, "_summary_ai_strict_positive_v11", False):
            _POSITIVE_RESULT_PATCHED = True
            return True
        _strict_result_executed._summary_ai_strict_positive_v11 = True  # type: ignore[attr-defined]
        _strict_result_executed._summary_ai_strict_positive_v1 = True  # type: ignore[attr-defined]
        _strict_result_executed._original = cur  # type: ignore[attr-defined]
        exec_mod._positive_result = _strict_result_executed
        _POSITIVE_RESULT_PATCHED = True
        logger.warning("[SUMMARY AI DIRECT DISPATCH] patched executor._positive_result strict version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] patch executor._positive_result failed")
        return False


def _install_executor_price_floor_patch(exec_mod: Any) -> bool:
    global _PRICE_FLOOR_PATCHED
    try:
        floor = _summary_ai_price_floor()
        if floor <= 0:
            return False
        try:
            exec_mod.DEFAULT_MIN_PRICE_FOR_ENTRY = float(floor)
        except Exception:
            pass
        cur = getattr(exec_mod, "_entry_price_bounds", None)
        if getattr(cur, "_summary_ai_price2500_patch_v11", False):
            _PRICE_FLOOR_PATCHED = True
            return True
        if not callable(cur):
            return False

        def _patched_entry_price_bounds():
            min_price, max_price, diag = cur()
            try:
                old_min = float(min_price or 0.0)
                min_price = max(old_min, float(floor))
                if not isinstance(diag, dict):
                    diag = {"raw_diag": str(diag)}
                diag = dict(diag)
                diag["summary_ai_price2500_patch"] = True
                diag["old_min_price"] = old_min
                diag["effective_min_price"] = float(min_price or 0.0)
                diag["min_override"] = float(floor)
            except Exception:
                pass
            return min_price, max_price, diag

        _patched_entry_price_bounds._summary_ai_price2500_patch_v11 = True  # type: ignore[attr-defined]
        _patched_entry_price_bounds._summary_ai_price2500_patch_v1 = True  # type: ignore[attr-defined]
        _patched_entry_price_bounds._original = cur  # type: ignore[attr-defined]
        exec_mod._entry_price_bounds = _patched_entry_price_bounds
        _PRICE_FLOOR_PATCHED = True
        logger.warning("[SUMMARY AI DIRECT DISPATCH] patched executor price floor min_price=%.0f version=%s", floor, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] patch executor price floor failed")
        return False


def _patch_once(*, log_patch: bool = True) -> bool:
    global _INSTALLED
    try:
        from trading.entry.summary_ai import executor as exec_mod
        _install_executor_positive_result_patch(exec_mod)
        _install_executor_price_floor_patch(exec_mod)
        _INSTALLED = True
        if log_patch:
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] patched strict_positive=%s price_floor=%.0f price_patch=%s version=%s",
                _POSITIVE_RESULT_PATCHED, _summary_ai_price_floor(), _PRICE_FLOOR_PATCHED, VERSION,
            )
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] patch_once failed")
        return False


def _watch_reinstall() -> None:
    loops = max(1, _env_int("SUMMARY_AI_DIRECT_DISPATCH_WATCH_LOOPS", 12))
    sleep_sec = max(0.5, _env_float("SUMMARY_AI_DIRECT_DISPATCH_WATCH_SLEEP_SEC", 2.0))
    for i in range(loops):
        ok = _patch_once(log_patch=False)
        if i in (0, loops - 1):
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] enforce i=%s/%s ok=%s strict_positive=%s price_floor=%.0f price_patch=%s version=%s",
                i, loops, ok, _POSITIVE_RESULT_PATCHED, _summary_ai_price_floor(), _PRICE_FLOOR_PATCHED, VERSION,
            )
        time.sleep(sleep_sec)


def install() -> bool:
    global _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC", True):
        logger.warning("[SUMMARY AI DIRECT DISPATCH] disabled by env")
        return False
    ok = _patch_once()
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch_reinstall, daemon=True, name="summary-ai-direct-dispatch-enforcer").start()
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] installed/enforcing (execute_ai_ok_entries_bulk dispatch inlined) ok=%s watcher=%s loops=%s sleep=%s strict_positive=%s price_floor=%.0f price_patch=%s version=%s",
            ok, _WATCHER_STARTED,
            _env_int("SUMMARY_AI_DIRECT_DISPATCH_WATCH_LOOPS", 12),
            _env_float("SUMMARY_AI_DIRECT_DISPATCH_WATCH_SLEEP_SEC", 2.0),
            _POSITIVE_RESULT_PATCHED,
            _summary_ai_price_floor(),
            _PRICE_FLOOR_PATCHED,
            VERSION,
        )
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI DIRECT DISPATCH] auto install failed")


__all__ = ["install", "VERSION"]
