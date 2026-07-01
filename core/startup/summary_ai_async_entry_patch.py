# ============================================================
# File   : core/startup/summary_ai_async_entry_patch.py
# Version: Ver11-DIRECT-SNAPSHOT-REENTRANT-LOCK
# ------------------------------------------------------------
# SUMMARY AI の実発注を非同期化しつつ、古い承認候補を何度も再実行して
# no order を増やす問題を防ぐ。
#
# Ver11:
#   - summary_entry.execute_entry_pipeline が run_entry_pipeline 内から呼ばれると、
#     entry_controller._pipeline_lock は既に保持済みになる。
#   - direct snapshot が同じlockを再取得しようとして
#     entry_controller_lock_timeout になり発注まで進まない問題を修正。
#   - lock busy時は既存パイプライン内のreentrant実行として、候補評価/発注へ進む。
#   - stale/retry/queueの既定はVer10を継続。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from collections import deque, defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EXECUTE = None
_ORIG_SUMMARY_ENTRY_PIPELINE = None
_ASYNC_LOCK = threading.Lock()
_QUEUE: deque[dict[str, Any]] = deque()
_WORKER_RUNNING = False
_SEQ = 0

os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", "0")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", "0")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", "20")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", "2")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", "1.0")
os.environ.setdefault("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", "1")
os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", "1")

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
        for _ in range(10):
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
        for _ in range(12):
            if not isinstance(cur, dict):
                break
            for k in ("skip_reason", "lock_wait_reason", "reason", "status"):
                r = cur.get(k)
                if r:
                    reasons.append(str(r))
            nxt = cur.get("result") or cur.get("pipeline_result")
            if not isinstance(nxt, dict):
                break
            cur = nxt
        return "|".join(reasons)
    except Exception:
        return ""


def _pending_counts(result: Any) -> tuple[int, int]:
    try:
        cur = result
        for _ in range(10):
            if isinstance(cur, dict):
                before = cur.get("pending_count_before")
                after = cur.get("pending_count_after")
                if before is not None or after is not None:
                    return int(before or 0), int(after or 0)
                cur = cur.get("result") or cur.get("pipeline_result")
                continue
            break
    except Exception:
        pass
    return 0, 0


def _is_retryable_controller_busy(result: Any) -> bool:
    try:
        text = _skip_reason(result).lower()
        hard_no_retry = (
            "snapshot_no_order",
            "summary_entry_executor_no_order",
            "no_tradable_rows_after_filters",
            "entry_pipeline_no_order",
        )
        if any(x in text for x in hard_no_retry):
            return False
        unwrapped = _unwrap_result(result)
        retryable = bool(unwrapped.get("retryable")) if isinstance(unwrapped, dict) else False
        before, after = _pending_counts(result)
        retry_markers = (
            "lock_timeout",
            "entry_controller_lock_timeout",
            "pending_moved_without_order",
            "order_id_empty_retryable",
            "already_running",
            "queued_async",
            "pipeline_busy",
        )
        if retryable:
            return True
        if any(x in text for x in retry_markers):
            return True
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


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _entries_to_list(entries: Any) -> list[dict[str, Any]]:
    try:
        from trading.summary import summary_entry as se
        fn = getattr(se, "normalize_approved_rows", None)
        if callable(fn):
            rows = fn(entries)
        else:
            rows = list(entries or []) if isinstance(entries, (list, tuple)) else ([entries] if isinstance(entries, dict) else [])
        out: list[dict[str, Any]] = []
        for r in rows or []:
            if isinstance(r, dict):
                d = dict(r)
                sym = _norm_symbol(d.get("symbol"))
                if sym:
                    d["symbol"] = sym
                    out.append(d)
        return out
    except Exception:
        logger.exception("[SUMMARY AI DIRECT SNAPSHOT] entries normalize failed")
        return []


def _build_boost_active(ec: Any) -> bool:
    try:
        gd = getattr(ec, "global_data")
        return bool(ec.boost_engine.update(
            win_rate=getattr(gd, "recent_win_rate", 0.5),
            regime=getattr(gd, "current_regime", 0),
            drawdown=getattr(gd, "current_drawdown", 0.0),
            collapse_prob=getattr(gd, "collapse_prob", 0.0),
            consecutive_losses=getattr(gd, "consecutive_losses", 0),
            regime_changed=getattr(gd, "regime_changed", False),
        ))
    except Exception:
        logger.debug("[SUMMARY AI DIRECT SNAPSHOT] boost update failed; fallback inactive", exc_info=True)
        return False


def _summary_ai_direct_snapshot_execute(entries: Any, *, pipeline_source: str | None = None, interval: int | None = None) -> dict[str, Any]:
    rows = _entries_to_list(entries)
    if not rows:
        return {"executed": False, "entries": 0, "skip_reason": "no_entries", "result": None}
    try:
        import trading.handlers.entry_controller as ec
        from trading.entry.pending_manager import pop_entry, snapshot_root
    except Exception:
        logger.exception("[SUMMARY AI DIRECT SNAPSHOT] import entry_controller failed")
        if callable(_ORIG_SUMMARY_ENTRY_PIPELINE):
            return _ORIG_SUMMARY_ENTRY_PIPELINE(entries, pipeline_source=pipeline_source, interval=interval)
        return {"executed": False, "entries": len(rows), "skip_reason": "entry_controller_import_failed", "result": None, "retryable": True}

    if not _env_bool("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", True):
        if callable(_ORIG_SUMMARY_ENTRY_PIPELINE):
            return _ORIG_SUMMARY_ENTRY_PIPELINE(entries, pipeline_source=pipeline_source, interval=interval)
        return {"executed": False, "entries": len(rows), "skip_reason": "direct_snapshot_disabled_no_original", "result": None}

    lock = getattr(ec, "_pipeline_lock", None)
    acquired = False
    reentrant_lock = False
    try:
        if lock is not None:
            acquired = bool(lock.acquire(blocking=False))
            if not acquired:
                if _env_bool("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", True):
                    reentrant_lock = True
                    logger.warning(
                        "[SUMMARY AI DIRECT SNAPSHOT] entry_controller lock busy -> reentrant execution entries=%s symbols=%s",
                        len(rows),
                        _symbols(rows),
                    )
                else:
                    logger.warning("[SUMMARY AI DIRECT SNAPSHOT] entry_controller lock busy entries=%s symbols=%s", len(rows), _symbols(rows))
                    return {"executed": False, "entries": len(rows), "skip_reason": "entry_controller_lock_timeout", "retryable": True, "result": None, "pending_root": snapshot_root()}
        try:
            market_open = bool(ec.is_market_open()) and bool(ec._is_trading_hours())
        except Exception:
            market_open = True
        if not market_open:
            return {"executed": False, "entries": len(rows), "skip_reason": "market_closed", "result": None}
        if pipeline_source:
            pipeline_source = ec._normalize_source(pipeline_source)
        if interval is not None:
            interval = ec._normalize_interval(interval)
        if pipeline_source and pipeline_source not in getattr(ec, "PIPELINE_SOURCE", set()):
            return {"executed": False, "entries": len(rows), "skip_reason": "invalid_pipeline_source", "result": None}
        if ec._api_rate_limited():
            return {"executed": False, "entries": len(rows), "skip_reason": "api_rate_limit", "retryable": True, "result": None}
        if not ec.ai_health_ok():
            return {"executed": False, "entries": len(rows), "skip_reason": "ai_health_ng", "result": None}
        if not ec.risk_ok():
            return {"executed": False, "entries": len(rows), "skip_reason": "risk_guard_ng", "result": None}
        if ec.detect_index_shock() != 0:
            return {"executed": False, "entries": len(rows), "skip_reason": "index_shock", "result": None}
        if not ec.allow_entry_by_market(now=dt.datetime.now(), nikkei_velocity=getattr(ec.global_data, "nikkei_velocity", None), api_429_count=getattr(ec.global_data, "api_429_count", 0), board_update_delay_sec=getattr(ec.global_data, "board_delay_sec", None)):
            return {"executed": False, "entries": len(rows), "skip_reason": "market_guard_ng", "result": None}

        ec.reset_entry_lock()
        boost_active = _build_boost_active(ec)
        open_position_symbols = ec._normalize_open_positions(getattr(ec.global_data, "open_positions", None))
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            sym = _norm_symbol(row.get("symbol"))
            if sym:
                row["symbol"] = sym
                by_symbol[sym].append(row)

        global_scored_candidates: list[dict[str, Any]] = []
        for symbol, bucket in by_symbol.items():
            try:
                scored = ec._build_scored_candidates(
                    symbol=symbol,
                    entries=sorted(bucket, key=lambda e: (ec.ENTRY_TYPE_PRIORITY.get(e.get("entry_type"), 0), ec._safe_float(e.get("score"), 0.0), e.get("created_at") or dt.datetime.min), reverse=True),
                    open_position_symbols=open_position_symbols,
                    boost_active=boost_active,
                    pipeline_source=pipeline_source,
                    interval=interval,
                )
                if scored:
                    global_scored_candidates.extend(scored)
            except Exception:
                logger.exception("[SUMMARY AI DIRECT SNAPSHOT] candidate build failed symbol=%s", symbol)
        if not global_scored_candidates:
            logger.info("[SUMMARY AI DIRECT SNAPSHOT] no candidates after evaluation entries=%s root=%s", len(rows), snapshot_root())
            return {"executed": False, "entries": len(rows), "approved_count": 0, "skip_reason": "snapshot_no_ai_approved_candidates", "retryable": False, "result": None, "pending_root": snapshot_root()}

        global_scored_candidates.sort(key=lambda x: (x.get("priority_score", 0.0), x.get("confidence", 0.0)), reverse=True)
        max_per_run = int(getattr(ec, "MAX_APPROVED_PER_RUN", 3) or 3)
        approved_count = 0
        attempted_count = 0
        executed_symbols: set[str] = set()
        order_results: list[dict[str, Any]] = []
        logger.warning(
            "[SUMMARY AI DIRECT SNAPSHOT] ranked total=%s top=%s pipeline_source=%s interval=%s reentrant_lock=%s",
            len(global_scored_candidates),
            [{"symbol": x.get("symbol"), "side": x.get("side"), "priority": round(ec._safe_float(x.get("priority_score"), 0.0), 4), "conf": round(ec._safe_float(x.get("confidence"), 0.0), 4)} for x in global_scored_candidates[:10]],
            pipeline_source,
            interval,
            reentrant_lock,
        )
        for item in global_scored_candidates:
            if approved_count >= max_per_run:
                break
            symbol = item.get("symbol")
            side = item.get("side")
            entry = item.get("entry")
            if not symbol or symbol in executed_symbols:
                continue
            if symbol in open_position_symbols:
                continue
            if ec._is_symbol_trade_restricted(symbol):
                continue
            if not ec.lock_symbol(symbol):
                continue
            attempted_count += 1
            ok = bool(ec._execute_best_candidate(item, boost_active=boost_active))
            order_results.append({"symbol": symbol, "side": side, "ok": ok})
            if not ok:
                continue
            approved_count += 1
            executed_symbols.add(symbol)
            try:
                pop_entry(symbol, entry)
            except Exception:
                logger.debug("[SUMMARY AI DIRECT SNAPSHOT] pop_entry failed symbol=%s", symbol, exc_info=True)
        executed = approved_count > 0
        out = {"executed": executed, "entries": len(rows), "approved_count": approved_count, "attempted_count": attempted_count, "result": order_results, "pipeline_source": pipeline_source, "interval": interval, "skip_reason": None if executed else "snapshot_no_order", "retryable": False, "pending_root": snapshot_root(), "reentrant_lock": reentrant_lock}
        logger.warning("[SUMMARY AI DIRECT SNAPSHOT] done %s", out)
        return out
    except Exception as e:
        logger.exception("[SUMMARY AI DIRECT SNAPSHOT] failed err=%s", e)
        return {"executed": False, "entries": len(rows), "skip_reason": "snapshot_entry_exception", "error": str(e), "retryable": False}
    finally:
        try:
            if acquired and lock is not None:
                lock.release()
        except Exception:
            logger.debug("[SUMMARY AI DIRECT SNAPSHOT] lock release failed", exc_info=True)


def _install_summary_entry_snapshot_patch() -> bool:
    global _ORIG_SUMMARY_ENTRY_PIPELINE
    try:
        from trading.summary import summary_entry as se
        current = getattr(se, "execute_entry_pipeline", None)
        if getattr(current, "_summary_ai_direct_snapshot_v11", False):
            logger.warning("[SUMMARY AI DIRECT SNAPSHOT] already installed v11")
            return True
        _ORIG_SUMMARY_ENTRY_PIPELINE = getattr(current, "_original", None) or current
        if not callable(_ORIG_SUMMARY_ENTRY_PIPELINE):
            logger.warning("[SUMMARY AI DIRECT SNAPSHOT] original summary entry pipeline missing")
            return False

        def _patched_execute_entry_pipeline(entries, *, pipeline_source: str | None = None, interval: int | None = None):
            return _summary_ai_direct_snapshot_execute(entries, pipeline_source=pipeline_source, interval=interval)

        _patched_execute_entry_pipeline._summary_ai_direct_snapshot_v11 = True  # type: ignore[attr-defined]
        _patched_execute_entry_pipeline._summary_ai_direct_snapshot_v10 = True  # type: ignore[attr-defined]
        _patched_execute_entry_pipeline._original = _ORIG_SUMMARY_ENTRY_PIPELINE  # type: ignore[attr-defined]
        se.execute_entry_pipeline = _patched_execute_entry_pipeline
        logger.warning("[SUMMARY AI DIRECT SNAPSHOT] installed v11 enabled=%s reentrant_lock=%s", _env_bool("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", True), _env_bool("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", True))
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT SNAPSHOT] install failed")
        return False


def _execute_original(item: dict[str, Any]) -> Any:
    return _ORIG_EXECUTE(item["ai_results"], df_summary=item["df_summary"], interval=item["interval"], max_entries=item["max_entries"], dry_run=False, require_market_open=item["require_market_open"], entry_pipeline=item["entry_pipeline"])


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
        stale_sec = _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 20.0)
        try:
            if stale_sec > 0 and age_sec > stale_sec:
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker skip stale seq=%s interval=%s age=%.3fs stale_sec=%.3f approved=%s symbols=%s queue_left=%s", seq, interval, age_sec, stale_sec, len(approved_rows), _symbols(approved_rows), q_left)
                continue
            if bool(item.get("require_market_open")) and not _market_open_now():
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker skip market_closed seq=%s interval=%s age=%.3fs approved=%s symbols=%s queue_left=%s", seq, interval, age_sec, len(approved_rows), _symbols(approved_rows), q_left)
                continue
            if not callable(_ORIG_EXECUTE):
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker original executor missing seq=%s", seq)
                continue
            retry_enabled = _env_bool("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", True)
            retry_max = max(1, _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 2))
            retry_sleep = max(0.2, _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 1.0))
            result: Any = None
            for attempt in range(1, retry_max + 1):
                attempt_started = time.time()
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker start seq=%s interval=%s attempt=%s/%s age=%.3fs approved=%s symbols=%s queue_left=%s", seq, interval, attempt, retry_max, max(0.0, attempt_started - queued_at), len(approved_rows), _symbols(approved_rows), q_left)
                result = _execute_original(item)
                summary = _summarize_result(result)
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker attempt done seq=%s interval=%s attempt=%s/%s elapsed=%.3fs summary=%s", seq, interval, attempt, retry_max, time.time() - attempt_started, summary)
                if bool(summary.get("executed")):
                    break
                if not retry_enabled or not _is_retryable_controller_busy(result):
                    break
                age_now = max(0.0, time.time() - queued_at)
                if stale_sec > 0 and age_now + retry_sleep > stale_sec:
                    logger.warning("[SUMMARY AI ASYNC ENTRY] retry stop stale risk seq=%s attempt=%s age=%.3fs stale_sec=%.3f", seq, attempt, age_now, stale_sec)
                    break
                if attempt < retry_max:
                    logger.warning("[SUMMARY AI ASYNC ENTRY] retry because controller busy seq=%s next_attempt=%s sleep=%.2fs symbols=%s reason_chain=%s", seq, attempt + 1, retry_sleep, _symbols(approved_rows), summary.get("reason_chain"))
                    time.sleep(retry_sleep)
            logger.warning("[SUMMARY AI ASYNC ENTRY] worker done seq=%s interval=%s elapsed=%.3fs final_summary=%s", seq, interval, time.time() - started, _summarize_result(result))
        except Exception as e:
            logger.exception("[SUMMARY AI ASYNC ENTRY] worker failed seq=%s err=%s", seq, e)


def _ensure_worker_started() -> None:
    global _WORKER_RUNNING
    with _ASYNC_LOCK:
        if _WORKER_RUNNING:
            return
        _WORKER_RUNNING = True
    t = threading.Thread(target=_run_worker_loop, name="SummaryAiAsyncEntry", daemon=True)
    t.start()


def _patched_execute_ai_ok_entries_bulk(ai_results, *, df_summary=None, interval=1, max_entries=3, dry_run=False, require_market_open=True, entry_pipeline=None):
    global _SEQ
    if dry_run or not _env_bool("SUMMARY_AI_ASYNC_ENTRY", True):
        if callable(_ORIG_EXECUTE):
            return _ORIG_EXECUTE(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)
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
            result = _ORIG_EXECUTE(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=False, require_market_open=require_market_open, entry_pipeline=entry_pipeline)
            logger.warning("[SUMMARY AI ASYNC ENTRY] direct sync done interval=%s elapsed=%.3fs summary=%s", interval, time.time() - started, _summarize_result(result))
            return result
        except Exception as e:
            logger.exception("[SUMMARY AI ASYNC ENTRY] direct sync failed err=%s", e)
            return {"executed": False, "submitted_async": False, "dry_run": False, "approved_rows": [], "result": None, "skip_reason": "direct_sync_exception"}
    queue_max = max(1, min(_env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1), 3))
    with _ASYNC_LOCK:
        dropped = 0
        while len(_QUEUE) >= queue_max:
            _QUEUE.popleft(); dropped += 1
        _SEQ += 1
        seq = _SEQ
        _QUEUE.append({"seq": seq, "queued_at": time.time(), "ai_results": ai_results, "df_summary": df_summary, "interval": interval, "max_entries": max_entries, "require_market_open": require_market_open, "entry_pipeline": entry_pipeline, "approved_rows": approved_rows})
        q_size = len(_QUEUE)
    _ensure_worker_started()
    if dropped:
        logger.warning("[SUMMARY AI ASYNC ENTRY] queued latest and dropped old count=%s seq=%s interval=%s", dropped, seq, interval)
    logger.warning("[SUMMARY AI ASYNC ENTRY] queued seq=%s interval=%s approved=%s symbols=%s queue_size=%s stale_sec=%.3f direct_sync=False executed=False submitted_async=True retry_busy=%s retry_max=%s retry_sleep=%.2f snapshot_direct=%s", seq, interval, len(approved_rows), _symbols(approved_rows), q_size, _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 20.0), _env_bool("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", True), _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 2), _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 1.0), _env_bool("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", True))
    return {"executed": False, "submitted_async": True, "queued_async": True, "async_seq": seq, "queue_size": q_size, "dry_run": False, "approved_rows": approved_rows, "result": {"status": "queued_async", "seq": seq, "queue_size": q_size}, "skip_reason": "queued_async"}


def install() -> bool:
    global _INSTALLED, _ORIG_EXECUTE
    if _INSTALLED:
        return True
    try:
        if os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC") is None or str(os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC")).strip() == "1":
            os.environ["SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"] = "0"
        os.environ["SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX"] = str(max(1, min(_env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1), 3)))
        if _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 20.0) > 20:
            os.environ["SUMMARY_AI_ASYNC_ENTRY_STALE_SEC"] = "20"
        if _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 2) > 2:
            os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX"] = "2"
        if _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 1.0) > 1.0:
            os.environ["SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC"] = "1.0"
        _install_summary_entry_snapshot_patch()
        from trading.entry.summary_ai import executor as exec_mod
        from trading.entry.summary_ai import runner as runner_mod
        current = getattr(exec_mod, "execute_ai_ok_entries_bulk", None)
        if getattr(current, "_summary_ai_async_entry_patch_v11", False):
            _INSTALLED = True
            logger.warning("[SUMMARY AI ASYNC ENTRY] already installed v11")
            return True
        _ORIG_EXECUTE = getattr(current, "_original", None) if getattr(current, "_summary_ai_async_entry_patch_v8", False) else current
        if not callable(_ORIG_EXECUTE):
            _ORIG_EXECUTE = current
        _patched_execute_ai_ok_entries_bulk._summary_ai_async_entry_patch_v11 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_async_entry_patch_v10 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_async_entry_patch_v9 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_async_entry_patch_v8 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._original = _ORIG_EXECUTE  # type: ignore[attr-defined]
        exec_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        runner_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        _INSTALLED = True
        logger.warning("[SUMMARY AI ASYNC ENTRY] installed v11 enabled=%s direct_sync=%s queue_max=%s stale_sec=%.3f latest_only=True queued_is_not_executed=True retry_busy=%s retry_max=%s retry_sleep=%.2f snapshot_direct=%s reentrant_lock=%s", _env_bool("SUMMARY_AI_ASYNC_ENTRY", True), _env_bool("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", False), _env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1), _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 20.0), _env_bool("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", True), _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 2), _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 1.0), _env_bool("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", True), _env_bool("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", True))
        return True
    except Exception:
        logger.exception("[SUMMARY AI ASYNC ENTRY] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ASYNC ENTRY] auto install failed")

__all__ = ["install"]
