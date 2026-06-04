# ============================================================
# File   : core/startup/summary_ai_async_entry_patch.py
# Version: Ver08-RETRY-NO-ORDER-PIPELINE-BUSY
# ------------------------------------------------------------
# Purpose:
#   SUMMARY AI の実発注で direct sync が 200秒超ブロックし、
#   summary parent / display / entry_controller lock を詰まらせる問題を防ぐ。
#
# Ver08:
#   - worker実行結果が entry_controller_lock_timeout だけでなく、
#     entry_controller_no_order / entry_pipeline_no_order / already_running 系でも
#     同じ approved を短時間リトライする。
#   - RANKING pipeline が一瞬先に entry_controller を握った場合でも、
#     Summary AI の承認済み候補を捨てずに注文化を再試行する。
#   - stale既定を90秒へ延長し、15時台の重いサマリー処理でもリトライ猶予を確保。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EXECUTE = None
_ASYNC_LOCK = threading.Lock()
_QUEUE: deque[dict[str, Any]] = deque()
_WORKER_RUNNING = False
_SEQ = 0

os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", "0")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", "0")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", "90")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", "8")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", "2.0")


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
        return max(1, int(float(v)))
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


def _safe_len(v: Any) -> int:
    try:
        return len(v) if v is not None else 0
    except Exception:
        return 0


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


def _unwrap_result(result: Any) -> Any:
    cur = result
    try:
        for _ in range(8):
            if isinstance(cur, dict) and isinstance(cur.get("result"), dict):
                cur = cur.get("result")
                continue
            break
    except Exception:
        return result
    return cur


def _skip_reason(result: Any) -> str:
    try:
        reasons: list[str] = []
        cur = result
        for _ in range(10):
            if not isinstance(cur, dict):
                break
            for k in ("skip_reason", "lock_wait_reason", "reason", "status"):
                r = cur.get(k)
                if r:
                    reasons.append(str(r))
            nxt = cur.get("result")
            if not isinstance(nxt, dict):
                break
            cur = nxt
        return "|".join(reasons)
    except Exception:
        return ""


def _pending_counts(result: Any) -> tuple[int, int]:
    try:
        cur = result
        for _ in range(8):
            if isinstance(cur, dict):
                before = cur.get("pending_count_before")
                after = cur.get("pending_count_after")
                if before is not None or after is not None:
                    return int(before or 0), int(after or 0)
                cur = cur.get("result")
                continue
            break
    except Exception:
        pass
    return 0, 0


def _is_retryable_controller_busy(result: Any) -> bool:
    try:
        text = _skip_reason(result).lower()
        unwrapped = _unwrap_result(result)
        retryable = bool(unwrapped.get("retryable")) if isinstance(unwrapped, dict) else False
        before, after = _pending_counts(result)
        busy_markers = (
            "lock_timeout",
            "entry_controller_lock_timeout",
            "entry_controller_no_order",
            "entry_controller_no_order_after_retry",
            "entry_pipeline_no_order",
            "already_running",
            "queued_async",
            "pipeline_busy",
        )
        if retryable:
            return True
        if any(x in text for x in busy_markers):
            return True
        # pending が残っていて executed=False の場合は、注文化できなかっただけなので再試行対象。
        if isinstance(result, dict) and not bool(result.get("executed")) and (before > 0 or after > 0):
            return True
        return False
    except Exception:
        return False


def _summarize_result(result: Any) -> dict[str, Any]:
    try:
        if isinstance(result, dict):
            return {
                "executed": bool(result.get("executed")),
                "submitted_async": bool(result.get("submitted_async")),
                "skip_reason": result.get("skip_reason"),
                "approved": _safe_len(result.get("approved_rows")),
                "retryable_controller_busy": _is_retryable_controller_busy(result),
                "reason_chain": _skip_reason(result),
                "result_type": type(result.get("result")).__name__,
                "result": result.get("result"),
            }
        return {"result_type": type(result).__name__, "result": result}
    except Exception as e:
        return {"summary_error": str(e), "result_type": type(result).__name__}


def _market_open_now() -> bool:
    try:
        from trading.entry.summary_ai import executor as exec_mod
        is_open = getattr(exec_mod, "is_market_open", None)
        if callable(is_open):
            return bool(is_open())
    except Exception:
        logger.debug("[SUMMARY AI ASYNC ENTRY] market open check failed", exc_info=True)
    return True


def _build_approved_rows(ai_results: Any, max_entries: int) -> list[Any]:
    try:
        from trading.entry.summary_ai import executor as exec_mod
        fn = getattr(exec_mod, "build_ai_ok_approved_rows", None)
        if callable(fn):
            return list(fn(ai_results, max_entries=max_entries) or [])
    except Exception:
        logger.exception("[SUMMARY AI ASYNC ENTRY] build approved rows failed")
    return []


def _execute_original(item: dict[str, Any]) -> Any:
    return _ORIG_EXECUTE(
        item["ai_results"],
        df_summary=item["df_summary"],
        interval=item["interval"],
        max_entries=item["max_entries"],
        dry_run=False,
        require_market_open=item["require_market_open"],
        entry_pipeline=item["entry_pipeline"],
    )


def _run_worker_loop() -> None:
    global _WORKER_RUNNING
    while True:
        with _ASYNC_LOCK:
            if not _QUEUE:
                _WORKER_RUNNING = False
                logger.info("[SUMMARY AI ASYNC ENTRY] queue worker idle")
                return
            item = _QUEUE.popleft()
            q_left = len(_QUEUE)

        seq = item["seq"]
        interval = item["interval"]
        approved_rows = item.get("approved_rows") or []
        started = time.time()
        queued_at = float(item.get("queued_at") or started)
        age_sec = max(0.0, started - queued_at)
        stale_sec = _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 90.0)

        try:
            if stale_sec > 0 and age_sec > stale_sec:
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] worker skip stale seq=%s interval=%s age=%.3fs stale_sec=%.3f approved=%s symbols=%s queue_left=%s",
                    seq, interval, age_sec, stale_sec, len(approved_rows), _symbols(approved_rows), q_left,
                )
                continue
            if bool(item.get("require_market_open")) and not _market_open_now():
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] worker skip market_closed seq=%s interval=%s age=%.3fs approved=%s symbols=%s queue_left=%s",
                    seq, interval, age_sec, len(approved_rows), _symbols(approved_rows), q_left,
                )
                continue
            if not callable(_ORIG_EXECUTE):
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker original executor missing seq=%s", seq)
                continue

            retry_enabled = _env_bool("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", True)
            retry_max = max(1, _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 8))
            retry_sleep = max(0.2, _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 2.0))
            result: Any = None

            for attempt in range(1, retry_max + 1):
                attempt_started = time.time()
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] worker start seq=%s interval=%s attempt=%s/%s age=%.3fs approved=%s symbols=%s queue_left=%s",
                    seq, interval, attempt, retry_max, max(0.0, attempt_started - queued_at), len(approved_rows), _symbols(approved_rows), q_left,
                )
                result = _execute_original(item)
                summary = _summarize_result(result)
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] worker attempt done seq=%s interval=%s attempt=%s/%s elapsed=%.3fs summary=%s",
                    seq, interval, attempt, retry_max, time.time() - attempt_started, summary,
                )

                if bool(summary.get("executed")):
                    break
                if not retry_enabled or not _is_retryable_controller_busy(result):
                    break

                age_now = max(0.0, time.time() - queued_at)
                if stale_sec > 0 and age_now + retry_sleep > stale_sec:
                    logger.warning(
                        "[SUMMARY AI ASYNC ENTRY] retry stop stale risk seq=%s attempt=%s age=%.3fs stale_sec=%.3f",
                        seq, attempt, age_now, stale_sec,
                    )
                    break
                if attempt < retry_max:
                    logger.warning(
                        "[SUMMARY AI ASYNC ENTRY] retry because entry_controller busy/no_order seq=%s next_attempt=%s sleep=%.2fs symbols=%s reason_chain=%s",
                        seq, attempt + 1, retry_sleep, _symbols(approved_rows), _skip_reason(result),
                    )
                    time.sleep(retry_sleep)

            logger.warning(
                "[SUMMARY AI ASYNC ENTRY] worker done seq=%s interval=%s elapsed=%.3fs final_summary=%s",
                seq, interval, time.time() - started, _summarize_result(result),
            )
        except Exception as e:
            logger.exception("[SUMMARY AI ASYNC ENTRY] worker failed seq=%s interval=%s err=%s", seq, interval, e)


def _ensure_worker_started() -> None:
    global _WORKER_RUNNING
    with _ASYNC_LOCK:
        if _WORKER_RUNNING:
            return
        _WORKER_RUNNING = True
        threading.Thread(target=_run_worker_loop, daemon=True, name="summary-ai-entry-async-queue-worker").start()


def _patched_execute_ai_ok_entries_bulk(
    ai_results,
    *,
    df_summary,
    interval=1,
    max_entries=10,
    dry_run=True,
    require_market_open=True,
    entry_pipeline=None,
):
    global _SEQ

    if dry_run or not _env_bool("SUMMARY_AI_ASYNC_ENTRY", True):
        if callable(_ORIG_EXECUTE):
            return _ORIG_EXECUTE(
                ai_results,
                df_summary=df_summary,
                interval=interval,
                max_entries=max_entries,
                dry_run=dry_run,
                require_market_open=require_market_open,
                entry_pipeline=entry_pipeline,
            )
        return {"executed": False, "submitted_async": False, "skip_reason": "original_executor_missing", "approved_rows": [], "result": None}

    approved_rows = _build_approved_rows(ai_results, max_entries=max_entries)
    if not approved_rows:
        logger.info("[SUMMARY AI ASYNC ENTRY] no approved rows; skip interval=%s", interval)
        return {"executed": False, "submitted_async": False, "dry_run": False, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok"}

    if require_market_open and not _market_open_now():
        logger.warning("[SUMMARY AI ASYNC ENTRY] market closed; skipped approved=%s symbols=%s", len(approved_rows), _symbols(approved_rows))
        return {"executed": False, "submitted_async": False, "dry_run": False, "approved_rows": approved_rows, "result": None, "skip_reason": "market_closed"}

    if _env_bool("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", False):
        started = time.time()
        logger.warning("[SUMMARY AI ASYNC ENTRY] direct sync start interval=%s max_entries=%s approved=%s symbols=%s", interval, max_entries, len(approved_rows), _symbols(approved_rows))
        try:
            result = _ORIG_EXECUTE(
                ai_results,
                df_summary=df_summary,
                interval=interval,
                max_entries=max_entries,
                dry_run=False,
                require_market_open=require_market_open,
                entry_pipeline=entry_pipeline,
            )
            logger.warning("[SUMMARY AI ASYNC ENTRY] direct sync done interval=%s elapsed=%.3fs summary=%s", interval, time.time() - started, _summarize_result(result))
            return result
        except Exception as e:
            logger.exception("[SUMMARY AI ASYNC ENTRY] direct sync failed err=%s", e)
            return {"executed": False, "submitted_async": False, "dry_run": False, "approved_rows": [], "result": None, "skip_reason": "direct_sync_exception"}

    queue_max = max(1, min(_env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1), 3))
    with _ASYNC_LOCK:
        dropped = 0
        while len(_QUEUE) >= queue_max:
            _QUEUE.popleft()
            dropped += 1
        _SEQ += 1
        seq = _SEQ
        _QUEUE.append({
            "seq": seq,
            "queued_at": time.time(),
            "ai_results": ai_results,
            "df_summary": df_summary,
            "interval": interval,
            "max_entries": max_entries,
            "require_market_open": require_market_open,
            "entry_pipeline": entry_pipeline,
            "approved_rows": approved_rows,
        })
        q_size = len(_QUEUE)

    _ensure_worker_started()
    if dropped:
        logger.warning("[SUMMARY AI ASYNC ENTRY] queued latest and dropped old count=%s seq=%s interval=%s", dropped, seq, interval)
    logger.warning(
        "[SUMMARY AI ASYNC ENTRY] queued seq=%s interval=%s approved=%s symbols=%s queue_size=%s stale_sec=%.3f direct_sync=False executed=False submitted_async=True retry_busy=%s retry_max=%s retry_sleep=%.2f",
        seq,
        interval,
        len(approved_rows),
        _symbols(approved_rows),
        q_size,
        _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 90.0),
        _env_bool("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", True),
        _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 8),
        _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 2.0),
    )
    return {
        "executed": False,
        "submitted_async": True,
        "queued_async": True,
        "async_seq": seq,
        "queue_size": q_size,
        "dry_run": False,
        "approved_rows": approved_rows,
        "result": {"status": "queued_async", "seq": seq, "queue_size": q_size},
        "skip_reason": "queued_async",
    }


def install() -> bool:
    global _INSTALLED, _ORIG_EXECUTE
    if _INSTALLED:
        return True

    try:
        if os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC") is None or str(os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC")).strip() == "1":
            os.environ["SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"] = "0"
        os.environ["SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX"] = str(max(1, min(_env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1), 3)))
        if _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 90.0) < 90:
            os.environ["SUMMARY_AI_ASYNC_ENTRY_STALE_SEC"] = "90"
        if _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 8) < 8:
            os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX"] = "8"

        from trading.entry.summary_ai import executor as exec_mod
        from trading.entry.summary_ai import runner as runner_mod

        current = getattr(exec_mod, "execute_ai_ok_entries_bulk", None)
        if getattr(current, "_summary_ai_async_entry_patch_v8", False):
            _INSTALLED = True
            logger.warning("[SUMMARY AI ASYNC ENTRY] already installed v8")
            return True
        _ORIG_EXECUTE = getattr(current, "_original", None) if getattr(current, "_summary_ai_async_entry_patch_v7", False) else current
        if not callable(_ORIG_EXECUTE):
            _ORIG_EXECUTE = current

        _patched_execute_ai_ok_entries_bulk._summary_ai_async_entry_patch_v8 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_async_entry_patch_v7 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._original = _ORIG_EXECUTE  # type: ignore[attr-defined]
        exec_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        runner_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI ASYNC ENTRY] installed v8 enabled=%s direct_sync=%s queue_max=%s stale_sec=%.3f latest_only=True queued_is_not_executed=True retry_busy=%s retry_max=%s retry_sleep=%.2f",
            _env_bool("SUMMARY_AI_ASYNC_ENTRY", True),
            _env_bool("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", False),
            _env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1),
            _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 90.0),
            _env_bool("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", True),
            _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 8),
            _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 2.0),
        )
        return True
    except Exception as e:
        logger.exception("[SUMMARY AI ASYNC ENTRY] install failed err=%s", e)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ASYNC ENTRY] auto install failed")

__all__ = ["install"]
