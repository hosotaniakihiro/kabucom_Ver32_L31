# ============================================================
# File   : core/startup/summary_ai_async_direct_dispatch_patch.py
# Version: V9-STRICT-EXECUTED-RESULT-AND-TIMEOUT
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が AI_OK / approved を出しても、
#   summary_ai_async_entry_patch が executed=False / skip=queued_async を返し、
#   実発注がworker待ち・stale skip になる問題を止める。
#
# V9:
#   - executor._positive_result が result['approved'] だけで True を返す問題を補正。
#   - entries=0 / result.executed=False / no_tradable_rows_after_filters を
#     外側 executed=True に誤変換しない。
#   - V8の direct_sync 強制 + direct snapshot timeout は維持。
#
# V8:
#   - V7の direct_sync 強制は維持。
#   - direct snapshot が entry_controller 内で長時間止まり、
#     sync fallback done が出ないケースを検出するためタイムアウトを追加。
#   - タイムアウト時は必ず direct snapshot timeout としてログに残す。
#   - timeout 後の重複発注を避けるため、同一呼び出しでは追加retryしない。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

VERSION = "V9-STRICT-EXECUTED-RESULT-AND-TIMEOUT"
_INSTALLED = False
_ORIG = None
_WATCHER_STARTED = False
_POSITIVE_RESULT_PATCHED = False

_RETRYABLE_NO_ORDER_MARKERS = (
    "queued_async",
    "snapshot_no_order",
    "entry_controller_no_order",
    "summary_entry_executor_no_order",
    "entry_pipeline_no_order",
    "pending_moved_without_order",
    "order_id_empty_retryable",
    "entry_controller_lock_timeout",
    "pipeline_busy",
    "already_running",
    "no_pending_registered",
    "pipeline_filter_mismatch",
)


def _force_direct_sync_env() -> None:
    os.environ["SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"] = "1"
    os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", "2")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", "0.7")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_PIPELINE_SOURCE", "SUMMARY")
    os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", "8.0")


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _symbols(rows: Any, limit: int = 20) -> list[str]:
    out: list[str] = []
    try:
        for r in list(rows or [])[:limit]:
            d = _row_to_dict(r)
            sym = str(d.get("symbol") or getattr(r, "symbol", "") or "").strip()
            if sym:
                out.append(sym)
    except Exception:
        pass
    return out


def _resolve_pipeline_source(rows: list[Any]) -> str:
    try:
        counts: dict[str, int] = {}
        for r in list(rows or [])[:50]:
            d = _row_to_dict(r)
            nested_ai = d.get("ai") if isinstance(d.get("ai"), dict) else {}
            nested_entry = d.get("entry") if isinstance(d.get("entry"), dict) else {}
            nested_row = d.get("entry_row") if isinstance(d.get("entry_row"), dict) else {}
            vals = [
                d.get("pipeline_source"), d.get("source"),
                nested_entry.get("pipeline_source"), nested_entry.get("source"),
                nested_row.get("pipeline_source"), nested_row.get("source"),
                nested_ai.get("pipeline_source"), nested_ai.get("source"),
                d.get("entry_type"), nested_entry.get("entry_type"), nested_ai.get("entry_type"),
            ]
            for v in vals:
                s = str(v or "").strip().upper()
                if not s:
                    continue
                if s in {"SUMMARY_AI", "AI"}:
                    continue
                if s in {"PUSH", "SUMMARY"}:
                    s = "SUMMARY"
                if s in {"SUMMARY", "RANKING", "TONOSAMA"}:
                    counts[s] = counts.get(s, 0) + 1
                    break
        if counts:
            return max(counts.items(), key=lambda kv: kv[1])[0]
    except Exception:
        logger.debug("[SUMMARY AI DIRECT DISPATCH] pipeline_source resolve failed", exc_info=True)
    return os.getenv("SUMMARY_AI_DIRECT_DISPATCH_PIPELINE_SOURCE", "SUMMARY").strip().upper() or "SUMMARY"


def _flatten_reasons(result: Any) -> str:
    reasons: list[str] = []
    seen: set[int] = set()

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        try:
            oid = id(v)
            if oid in seen:
                return
            seen.add(oid)
        except Exception:
            pass
        if isinstance(v, dict):
            for k in ("skip_reason", "reason", "status", "lock_wait_reason"):
                r = v.get(k)
                if r:
                    reasons.append(str(r))
            for k in ("result", "pipeline_result"):
                child = v.get(k)
                if child is not None and child is not v:
                    walk(child, depth + 1)
            if isinstance(v.get("result"), list):
                walk(v.get("result"), depth + 1)
        elif isinstance(v, (list, tuple, set)):
            for x in list(v)[:30]:
                walk(x, depth + 1)

    walk(result)
    return "|".join(reasons)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


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
            for key in ("result", "pipeline_result"):
                child = result.get(key)
                if child is not result and _strict_result_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_strict_result_executed(x) for x in result)
        return False
    except Exception:
        return False


def _result_executed(result: Any) -> bool:
    return _strict_result_executed(result)


def _is_queued_async(result: Any) -> bool:
    try:
        if not isinstance(result, dict):
            return False
        if bool(result.get("submitted_async")) or bool(result.get("queued_async")):
            return True
        child = result.get("result")
        if isinstance(child, dict) and str(child.get("status") or "").lower() == "queued_async":
            return True
    except Exception:
        pass
    return False


def _registered_count(result: Any) -> int:
    try:
        if isinstance(result, dict):
            direct = result.get("registered")
            if direct is not None:
                return _safe_int(direct, 0)
            for key in ("result", "pipeline_result"):
                n = _registered_count(result.get(key))
                if n > 0:
                    return n
    except Exception:
        pass
    return 0


def _is_retryable_no_order(result: Any) -> bool:
    try:
        if _result_executed(result):
            return False
        text = _flatten_reasons(result).lower()
        if any(x in text for x in _RETRYABLE_NO_ORDER_MARKERS):
            return True
        if _registered_count(result) > 0:
            return True
    except Exception:
        pass
    return False


def _is_timeout_result(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("timeout"))


def _call_with_timeout(label: str, rows: list[Any], timeout_sec: float, fn: Callable[[], Any]) -> Any:
    timeout_sec = float(timeout_sec or 0.0)
    if timeout_sec <= 0:
        return fn()
    box: dict[str, Any] = {"done": False, "result": None, "error": None}

    def _target() -> None:
        try:
            box["result"] = fn()
        except Exception as e:
            box["error"] = e
        finally:
            box["done"] = True

    symbols = _symbols(rows)
    started = time.time()
    th = threading.Thread(target=_target, daemon=True, name=f"summary-ai-direct-{label}")
    th.start()
    th.join(timeout_sec)
    elapsed = time.time() - started
    if th.is_alive():
        logger.error(
            "[SUMMARY AI DIRECT DISPATCH] %s timeout timeout=%.3fs elapsed=%.3fs symbols=%s version=%s note=inner_thread_left_daemon_to_avoid_blocking",
            label, timeout_sec, elapsed, symbols, VERSION,
        )
        return {"executed": False, "timeout": True, "skip_reason": f"{label}_timeout", "elapsed_sec": elapsed, "symbols": symbols}
    if box.get("error") is not None:
        raise box["error"]
    return box.get("result")


def _direct_snapshot_execute(approved_rows: list[Any], interval: Any) -> Any:
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True):
        return None
    try:
        from trading.summary import summary_entry as se
        fn = getattr(se, "execute_entry_pipeline", None)
        if not callable(fn):
            return None
        pipeline_source = _resolve_pipeline_source(approved_rows)
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] direct snapshot pipeline_source resolved=%s symbols=%s timeout=%.3fs version=%s",
            pipeline_source, _symbols(approved_rows), _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0), VERSION,
        )
        return fn(approved_rows, pipeline_source=pipeline_source, interval=interval)
    except TypeError:
        try:
            from trading.summary import summary_entry as se
            fn = getattr(se, "execute_entry_pipeline", None)
            if callable(fn):
                return fn(approved_rows)
        except Exception:
            logger.exception("[SUMMARY AI DIRECT DISPATCH] direct snapshot fallback failed")
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] direct snapshot failed")
    return None


def _fallback_direct_dispatch(result: Any, kwargs: dict[str, Any]) -> Any:
    try:
        if _result_executed(result) or not _is_queued_async(result):
            return result
        approved_rows = list(result.get("approved_rows") or []) if isinstance(result, dict) else []
        if not approved_rows:
            return result
        interval = kwargs.get("interval", 1)
        attempts = max(1, _env_int("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", 2))
        retry_sleep = max(0.3, _env_float("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", 0.7))
        timeout_sec = _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0)
        last_result: Any = None
        for attempt in range(1, attempts + 1):
            started = time.time()
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] sync fallback start attempt=%s/%s interval=%s approved=%s symbols=%s timeout=%.3fs version=%s",
                attempt, attempts, interval, len(approved_rows), _symbols(approved_rows), timeout_sec, VERSION,
            )
            snap_result = _call_with_timeout(
                "direct_snapshot", approved_rows, timeout_sec, lambda: _direct_snapshot_execute(approved_rows, interval)
            )
            last_result = snap_result
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] sync fallback done attempt=%s/%s elapsed=%.3fs executed=%s timeout=%s registered=%s retryable=%s reason_chain=%s result=%s",
                attempt, attempts, time.time() - started, _result_executed(snap_result), _is_timeout_result(snap_result),
                _registered_count(snap_result), _is_retryable_no_order(snap_result), _flatten_reasons(snap_result), snap_result,
            )
            if _result_executed(snap_result):
                break
            if _is_timeout_result(snap_result):
                break
            if not _is_retryable_no_order(snap_result):
                break
            if attempt < attempts:
                time.sleep(retry_sleep)
        if isinstance(result, dict):
            out = dict(result)
            out["direct_dispatch_sync_fallback"] = True
            out["direct_dispatch_result"] = last_result
            if _result_executed(last_result):
                out["executed"] = True
                out["skip_reason"] = None
            elif _is_timeout_result(last_result):
                out["executed"] = False
                out["skip_reason"] = "direct_snapshot_timeout"
                out["direct_dispatch_timeout"] = True
            else:
                out["executed"] = False
                out["skip_reason"] = _flatten_reasons(last_result) or "entry_pipeline_no_order"
            return out
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] sync fallback failed")
    return result


def _patched_execute_ai_ok_entries_bulk(*args: Any, **kwargs: Any):
    _force_direct_sync_env()
    result = _ORIG(*args, **kwargs)
    if bool(kwargs.get("dry_run", False)):
        return result
    return _fallback_direct_dispatch(result, kwargs)


def _install_executor_positive_result_patch(exec_mod: Any) -> bool:
    global _POSITIVE_RESULT_PATCHED
    try:
        cur = getattr(exec_mod, "_positive_result", None)
        if getattr(cur, "_summary_ai_strict_positive_v1", False):
            _POSITIVE_RESULT_PATCHED = True
            return True
        _strict_result_executed._summary_ai_strict_positive_v1 = True  # type: ignore[attr-defined]
        _strict_result_executed._original = cur  # type: ignore[attr-defined]
        exec_mod._positive_result = _strict_result_executed
        _POSITIVE_RESULT_PATCHED = True
        logger.warning("[SUMMARY AI DIRECT DISPATCH] patched executor._positive_result strict version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] patch executor._positive_result failed")
        return False


def _patch_once(*, log_patch: bool = True) -> bool:
    global _INSTALLED, _ORIG
    try:
        _force_direct_sync_env()
        from trading.entry.summary_ai import executor as exec_mod
        from trading.entry.summary_ai import runner as runner_mod
        _install_executor_positive_result_patch(exec_mod)
        cur = getattr(exec_mod, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            logger.debug("[SUMMARY AI DIRECT DISPATCH] target missing")
            return False
        if getattr(cur, "_summary_ai_direct_dispatch_v9", False):
            _INSTALLED = True
            return True
        _ORIG = getattr(cur, "_original", cur) if any(getattr(cur, f"_summary_ai_direct_dispatch_v{i}", False) for i in range(1, 9)) else cur
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v9 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v8 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v7 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v6 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._original = _ORIG  # type: ignore[attr-defined]
        exec_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        runner_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        _INSTALLED = True
        if log_patch:
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] patched v9 target=%s direct_sync_env=%s attempts=%s snapshot_first=%s timeout=%.3fs strict_positive=%s source_match=True version=%s",
                getattr(_ORIG, "__name__", type(_ORIG).__name__),
                os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"),
                _env_int("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", 2),
                _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True),
                _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0),
                _POSITIVE_RESULT_PATCHED,
                VERSION,
            )
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] patch_once failed")
        return False


def _watch_reinstall() -> None:
    loops = max(1, _env_int("SUMMARY_AI_DIRECT_DISPATCH_WATCH_LOOPS", 12))
    sleep_sec = max(0.5, _env_float("SUMMARY_AI_DIRECT_DISPATCH_WATCH_SLEEP_SEC", 2.0))
    for i in range(loops):
        _force_direct_sync_env()
        ok = _patch_once(log_patch=False)
        if i in (0, loops - 1):
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] enforce v9 i=%s/%s ok=%s direct_sync_env=%s timeout=%.3fs strict_positive=%s version=%s",
                i, loops, ok, os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"),
                _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0), _POSITIVE_RESULT_PATCHED, VERSION,
            )
        time.sleep(sleep_sec)


def install() -> bool:
    global _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC", True):
        logger.warning("[SUMMARY AI DIRECT DISPATCH] disabled by env")
        return False
    _force_direct_sync_env()
    ok = _patch_once()
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch_reinstall, daemon=True, name="summary-ai-direct-dispatch-enforcer").start()
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] installed/enforcing v9 ok=%s watcher=%s loops=%s sleep=%s direct_sync_env=%s snapshot_first=%s timeout=%.3fs strict_positive=%s version=%s",
            ok,
            _WATCHER_STARTED,
            _env_int("SUMMARY_AI_DIRECT_DISPATCH_WATCH_LOOPS", 12),
            _env_float("SUMMARY_AI_DIRECT_DISPATCH_WATCH_SLEEP_SEC", 2.0),
            os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"),
            _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True),
            _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0),
            _POSITIVE_RESULT_PATCHED,
            VERSION,
        )
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI DIRECT DISPATCH] auto install failed")


__all__ = ["install", "VERSION"]
