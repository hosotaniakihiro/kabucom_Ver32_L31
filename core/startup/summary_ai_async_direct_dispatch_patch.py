# ============================================================
# File   : core/startup/summary_ai_async_direct_dispatch_patch.py
# Version: V6-PIPELINE-SOURCE-MATCH
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が AI_OK を出しても、summary_ai_async_entry_patch が
#   executed=False / skip=queued_async を返したまま実際の entry_controller
#   dispatch が薄いケースを救済する。
#
# V6:
#   - direct snapshot 実行時に pipeline_source="SUMMARY_AI" 固定で渡していたため、
#     approved_rows 側の source="SUMMARY" と entry_controller の source filter が不一致になり、
#       ENTRY_SKIP reason=PIPELINE_FILTER_MISMATCH
#     で全件落ちる問題を修正。
#   - approved_rows の source / pipeline_source / entry_type から実sourceを解決し、
#     SUMMARY候補は pipeline_source="SUMMARY" で entry_controller へ渡す。
#
# V5:
#   - 古い direct dispatch thread を latest-only で破棄する。
#   - pending 登録前の stale skip / no_pending_registered を避けるため、
#     まず trading.summary.summary_entry.execute_entry_pipeline を直接実行する。
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
            if d:
                out.append(str(d.get("symbol") or ""))
            else:
                out.append(str(getattr(r, "symbol", "")))
    except Exception:
        pass
    return [x for x in out if x]


def _resolve_pipeline_source(rows: list[Any]) -> str:
    """entry_controller の source filter と一致する pipeline_source を解決する。"""
    try:
        counts: dict[str, int] = {}
        for r in list(rows or [])[:50]:
            d = _row_to_dict(r)
            nested_ai = d.get("ai") if isinstance(d.get("ai"), dict) else {}
            nested_entry = d.get("entry") if isinstance(d.get("entry"), dict) else {}
            nested_row = d.get("entry_row") if isinstance(d.get("entry_row"), dict) else {}
            vals = [
                d.get("pipeline_source"),
                d.get("source"),
                nested_entry.get("pipeline_source"),
                nested_entry.get("source"),
                nested_row.get("pipeline_source"),
                nested_row.get("source"),
                nested_ai.get("pipeline_source"),
                nested_ai.get("source"),
                d.get("entry_type"),
                nested_entry.get("entry_type"),
                nested_ai.get("entry_type"),
            ]
            for v in vals:
                s = str(v or "").strip().upper()
                if not s:
                    continue
                if s in {"SUMMARY_AI", "AI"}:
                    # SUMMARY_AI は entry type として扱い、source にはしない。
                    continue
                if s in {"SUMMARY", "PUSH"}:
                    s = "SUMMARY"
                if s in {"RANKING", "TONOSAMA", "SUMMARY"}:
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
            detail = v.get("result")
            if isinstance(detail, list):
                walk(detail, depth + 1)
        elif isinstance(v, (list, tuple, set)):
            for x in list(v)[:30]:
                walk(x, depth + 1)

    walk(result)
    return "|".join(reasons)


def _result_executed(result: Any) -> bool:
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return bool(result)
        if isinstance(result, dict):
            if bool(result.get("executed")):
                return True
            for key in ("executed_count", "order_count", "submitted_count", "sent_count", "approved_count"):
                try:
                    if int(result.get(key) or 0) > 0:
                        if key == "approved_count" and not bool(result.get("executed")):
                            continue
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


def _registered_count(result: Any) -> int:
    try:
        if isinstance(result, dict):
            direct = result.get("registered")
            if direct is not None:
                return int(direct or 0)
            for key in ("result", "pipeline_result"):
                child = result.get(key)
                n = _registered_count(child)
                if n > 0:
                    return n
        return 0
    except Exception:
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
        return False
    except Exception:
        return False


def _is_latest_seq(seq: int) -> bool:
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_LATEST_ONLY", True):
        return True
    try:
        with _LOCK:
            return int(seq) == int(_SEQ)
    except Exception:
        return True


def _direct_snapshot_execute(approved_rows: list[Any], interval: Any) -> Any:
    """pending を経由せず、SUMMARY AI 承認済み候補を直接 entry pipeline へ渡す。"""
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True):
        return None
    try:
        from trading.summary import summary_entry as se
        fn = getattr(se, "execute_entry_pipeline", None)
        if not callable(fn):
            return None
        pipeline_source = _resolve_pipeline_source(approved_rows)
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] direct snapshot pipeline_source resolved=%s symbols=%s",
            pipeline_source,
            _symbols(approved_rows),
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


def _direct_worker(seq: int, approved_rows: list[Any], df_summary: Any, interval: Any, max_attempts: int) -> None:
    delay = max(0.0, _env_float("SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC", 0.10))
    if delay > 0:
        time.sleep(delay)
    if not _is_latest_seq(seq):
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] skip old seq=%s latest_only=True symbols=%s",
            seq,
            _symbols(approved_rows),
        )
        return

    if _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True):
        started = time.time()
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] snapshot-first start seq=%s interval=%s approved=%s symbols=%s",
            seq,
            interval,
            len(approved_rows),
            _symbols(approved_rows),
        )
        snap_result = _direct_snapshot_execute(approved_rows, interval)
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] snapshot-first done seq=%s elapsed=%.3fs executed=%s reason_chain=%s result=%s",
            seq,
            time.time() - started,
            _result_executed(snap_result),
            _flatten_reasons(snap_result),
            snap_result,
        )
        if _result_executed(snap_result):
            return
        if snap_result is not None and not _is_retryable_no_order(snap_result):
            return

    try:
        from trading.summary.summary_entry import run_summary_entry_executor
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] import run_summary_entry_executor failed seq=%s", seq)
        return

    retry_sleep = max(0.3, _env_float("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", 0.7))
    attempts = max(1, max_attempts)
    last_result: Any = None

    for attempt in range(1, attempts + 1):
        if not _is_latest_seq(seq):
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] stop old seq before attempt seq=%s attempt=%s latest_only=True symbols=%s",
                seq,
                attempt,
                _symbols(approved_rows),
            )
            return
        started = time.time()
        try:
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] fallback start seq=%s attempt=%s/%s interval=%s approved=%s symbols=%s",
                seq,
                attempt,
                attempts,
                interval,
                len(approved_rows),
                _symbols(approved_rows),
            )
            result = run_summary_entry_executor(approved_rows, df_summary, interval)
            last_result = result
            executed = _result_executed(result)
            registered = _registered_count(result)
            reason_chain = _flatten_reasons(result)
            retryable = _is_retryable_no_order(result)
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] fallback done seq=%s attempt=%s/%s elapsed=%.3fs executed=%s registered=%s retryable_no_order=%s reason_chain=%s result=%s",
                seq,
                attempt,
                attempts,
                time.time() - started,
                executed,
                registered,
                retryable,
                reason_chain,
                result,
            )
            if executed:
                return
            if not retryable:
                return
        except Exception:
            logger.exception("[SUMMARY AI DIRECT DISPATCH] fallback failed seq=%s attempt=%s/%s", seq, attempt, attempts)
        if attempt < attempts:
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] retry no-order seq=%s next_attempt=%s/%s sleep=%.2fs symbols=%s last_reason=%s",
                seq,
                attempt + 1,
                attempts,
                retry_sleep,
                _symbols(approved_rows),
                _flatten_reasons(last_result),
            )
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
        threading.Thread(
            target=_direct_worker,
            args=(seq, approved_rows, df_summary, interval, max_attempts),
            daemon=True,
            name=f"summary-ai-direct-dispatch-{seq}",
        ).start()
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] scheduled seq=%s interval=%s approved=%s symbols=%s original_skip=%s attempts=%s latest_only=%s snapshot_first=%s",
            seq,
            interval,
            len(approved_rows),
            _symbols(approved_rows),
            result.get("skip_reason") if isinstance(result, dict) else None,
            max_attempts,
            _env_bool("SUMMARY_AI_DIRECT_DISPATCH_LATEST_ONLY", True),
            _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True),
        )
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
        if getattr(cur, "_summary_ai_direct_dispatch_v6", False):
            _INSTALLED = True
            return True
        _ORIG = getattr(cur, "_original", cur) if getattr(cur, "_summary_ai_direct_dispatch_v5", False) or getattr(cur, "_summary_ai_direct_dispatch_v4", False) else cur
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v1 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v2 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v3 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v4 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v5 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v6 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._original = _ORIG  # type: ignore[attr-defined]
        exec_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        runner_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        _INSTALLED = True
        if log_patch:
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] patched v6 target=%s delay=%.2fs attempts=%s latest_only=%s snapshot_first=%s source_match=True",
                getattr(_ORIG, "__name__", type(_ORIG).__name__),
                _env_float("SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC", 0.10),
                _env_int("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", 2),
                _env_bool("SUMMARY_AI_DIRECT_DISPATCH_LATEST_ONLY", True),
                _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True),
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
            logger.warning("[SUMMARY AI DIRECT DISPATCH] enforce v6 i=%s/%s ok=%s", i, loops, ok)
        time.sleep(sleep_sec)


def install() -> bool:
    global _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC", True):
        logger.warning("[SUMMARY AI DIRECT DISPATCH] disabled by env")
        return False
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_LATEST_ONLY", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_DELAY_SEC", "0.10")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", "2")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", "0.7")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_PIPELINE_SOURCE", "SUMMARY")
    if _INSTALLED and _WATCHER_STARTED:
        return True
    ok = _patch_once()
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch_reinstall, daemon=True, name="summary-ai-direct-dispatch-enforcer").start()
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] installed/enforcing v6 ok=%s watcher=%s loops=%s sleep=%s latest_only=%s snapshot_first=%s source_match=True",
            ok,
            _WATCHER_STARTED,
            _env_int("SUMMARY_AI_DIRECT_DISPATCH_WATCH_LOOPS", 12),
            _env_float("SUMMARY_AI_DIRECT_DISPATCH_WATCH_SLEEP_SEC", 2.0),
            _env_bool("SUMMARY_AI_DIRECT_DISPATCH_LATEST_ONLY", True),
            _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True),
        )
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI DIRECT DISPATCH] auto install failed")

__all__ = ["install"]
