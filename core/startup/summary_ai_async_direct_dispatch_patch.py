# ============================================================
# File   : core/startup/summary_ai_async_direct_dispatch_patch.py
# Version: V3-FAST-STARTUP-IDEMPOTENT-WATCHER
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が AI_OK を出しても、summary_ai_async_entry_patch が
#   executed=False / skip=queued_async を返したまま実際の entry_controller
#   dispatch が薄いケースを救済する。
#
# V3:
#   - 起動遅延対策として watch loop を 180回×0.5秒 から既定 12回×2秒へ短縮。
#   - install済みなら install() は静かに即return。
#   - enforceログも最小限にする。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG = None
_SEQ = 0
_LOCK = threading.Lock()
_WATCHER_STARTED = False


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


def _symbols(rows: Any, limit: int = 20) -> list[str]:
    out: list[str] = []
    try:
        for r in list(rows or [])[:limit]:
            if isinstance(r, dict):
                out.append(str(r.get("symbol") or ""))
            else:
                out.append(str(getattr(r, "symbol", "")))
    except Exception:
        pass
    return [x for x in out if x]


def _result_executed(result: Any) -> bool:
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return bool(result)
        if isinstance(result, dict):
            if bool(result.get("executed")):
                return True
            for key in ("executed_count", "order_count", "submitted_count", "sent_count"):
                try:
                    if int(result.get(key) or 0) > 0:
                        return True
                except Exception:
                    pass
            for key in ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols"):
                v = result.get(key)
                if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                    return True
                if v and not isinstance(v, (list, tuple, set, dict)):
                    return True
            for key in ("result", "pipeline_result"):
                child = result.get(key)
                if child is not result and _result_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_result_executed(x) for x in result)
        return False
    except Exception:
        return False


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


def _direct_worker(seq: int, approved_rows: list[Any], df_summary: Any, interval: Any, max_attempts: int) -> None:
    delay = max(0.0, _env_float("SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC", 0.35))
    if delay > 0:
        time.sleep(delay)
    try:
        from trading.summary.summary_entry import run_summary_entry_executor
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] import run_summary_entry_executor failed seq=%s", seq)
        return
    retry_sleep = max(0.5, _env_float("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", 2.0))
    for attempt in range(1, max(1, max_attempts) + 1):
        started = time.time()
        try:
            logger.warning("[SUMMARY AI DIRECT DISPATCH] start seq=%s attempt=%s/%s interval=%s approved=%s symbols=%s", seq, attempt, max_attempts, interval, len(approved_rows), _symbols(approved_rows))
            result = run_summary_entry_executor(approved_rows, df_summary, interval)
            executed = _result_executed(result)
            logger.warning("[SUMMARY AI DIRECT DISPATCH] done seq=%s attempt=%s/%s elapsed=%.3fs executed=%s result=%s", seq, attempt, max_attempts, time.time() - started, executed, result)
            if executed:
                return
            if isinstance(result, dict) and int(result.get("registered") or 0) > 0:
                return
        except Exception:
            logger.exception("[SUMMARY AI DIRECT DISPATCH] failed seq=%s attempt=%s/%s", seq, attempt, max_attempts)
        if attempt < max_attempts:
            time.sleep(retry_sleep)


def _patched_execute_ai_ok_entries_bulk(*args: Any, **kwargs: Any):
    global _SEQ
    result = _ORIG(*args, **kwargs)
    try:
        if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC", True):
            return result
        if bool(kwargs.get("dry_run", True)):
            return result
        if _result_executed(result) or not _is_queued_async(result):
            return result
        approved_rows = list(result.get("approved_rows") or []) if isinstance(result, dict) else []
        if not approved_rows:
            return result
        df_summary = kwargs.get("df_summary")
        interval = kwargs.get("interval", 1)
        max_attempts = max(1, _env_int("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", 2))
        with _LOCK:
            _SEQ += 1
            seq = _SEQ
        threading.Thread(target=_direct_worker, args=(seq, approved_rows, df_summary, interval, max_attempts), daemon=True, name=f"summary-ai-direct-dispatch-{seq}").start()
        logger.warning("[SUMMARY AI DIRECT DISPATCH] scheduled seq=%s interval=%s approved=%s symbols=%s original_skip=%s", seq, interval, len(approved_rows), _symbols(approved_rows), result.get("skip_reason") if isinstance(result, dict) else None)
        if isinstance(result, dict):
            out = dict(result)
            out["direct_dispatch_scheduled"] = True
            out["direct_dispatch_seq"] = seq
            return out
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] wrapper failed")
    return result


def _patch_once(*, log_patch: bool = True) -> bool:
    global _INSTALLED, _ORIG
    try:
        from trading.entry.summary_ai import executor as exec_mod
        from trading.entry.summary_ai import runner as runner_mod
        cur = getattr(exec_mod, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            logger.debug("[SUMMARY AI DIRECT DISPATCH] target missing")
            return False
        if getattr(cur, "_summary_ai_direct_dispatch_v3", False) or getattr(cur, "_summary_ai_direct_dispatch_v2", False):
            _INSTALLED = True
            return True
        _ORIG = cur
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v1 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v2 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v3 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._original = cur  # type: ignore[attr-defined]
        exec_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        runner_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        _INSTALLED = True
        if log_patch:
            logger.warning("[SUMMARY AI DIRECT DISPATCH] patched target=%s delay=%.2fs attempts=%s", getattr(cur, "__name__", type(cur).__name__), _env_float("SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC", 0.35), _env_int("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", 2))
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
            logger.warning("[SUMMARY AI DIRECT DISPATCH] enforce v3 i=%s/%s ok=%s", i, loops, ok)
        time.sleep(sleep_sec)


def install() -> bool:
    global _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC", True):
        logger.warning("[SUMMARY AI DIRECT DISPATCH] disabled by env")
        return False
    if _INSTALLED and _WATCHER_STARTED:
        return True
    ok = _patch_once()
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch_reinstall, daemon=True, name="summary-ai-direct-dispatch-enforcer").start()
        logger.warning("[SUMMARY AI DIRECT DISPATCH] installed/enforcing v3 ok=%s watcher=%s loops=%s sleep=%s", ok, _WATCHER_STARTED, _env_int("SUMMARY_AI_DIRECT_DISPATCH_WATCH_LOOPS", 12), _env_float("SUMMARY_AI_DIRECT_DISPATCH_WATCH_SLEEP_SEC", 2.0))
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI DIRECT DISPATCH] auto install failed")

__all__ = ["install"]
