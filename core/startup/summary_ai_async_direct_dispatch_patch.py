# ============================================================
# File   : core/startup/summary_ai_async_direct_dispatch_patch.py
# Version: V11-DIRECT-DISPATCH-ROLLING-AI-OK-CANDIDATES
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が AI_OK / approved を出しても、実発注が
#   queued_async / snapshot_no_order / entry_controller_no_order で止まる問題を止める。
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
from typing import Any, Callable

logger = logging.getLogger(__name__)

VERSION = "V11-DIRECT-DISPATCH-ROLLING-AI-OK-CANDIDATES"
_INSTALLED = False
_ORIG = None
_WATCHER_STARTED = False
_POSITIVE_RESULT_PATCHED = False
_PRICE_FLOOR_PATCHED = False

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
    "no_tradable_rows_after_filters",
)

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
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SCAN_LIMIT", "12")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_BATCH_SIZE", "3")

    floor = _summary_ai_price_floor()
    if floor > 0:
        os.environ["SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE"] = str(floor)
        os.environ["SUMMARY_AI_ENTRY_MIN_PRICE"] = str(floor)
        os.environ["ENTRY_MIN_PRICE"] = str(floor)
        os.environ["SUMMARY_AI_LIQ_MIN_PRICE"] = str(floor)


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


def _pick_symbol(row: Any) -> str:
    d = _row_to_dict(row)
    return str(d.get("symbol") or d.get("Symbol") or getattr(row, "symbol", "") or "").strip()


def _symbols(rows: Any, limit: int = 20) -> list[str]:
    out: list[str] = []
    try:
        for r in list(rows or [])[:limit]:
            sym = _pick_symbol(r)
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
            for k in ("result", "pipeline_result", "direct_dispatch_result"):
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
            for key in ("result", "pipeline_result", "direct_dispatch_result"):
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
            "[SUMMARY AI DIRECT DISPATCH] direct snapshot pipeline_source resolved=%s symbols=%s timeout=%.3fs price_floor=%.0f version=%s",
            pipeline_source, _symbols(approved_rows), _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0), _summary_ai_price_floor(), VERSION,
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


def _batch_size() -> int:
    return max(1, min(_env_int("SUMMARY_AI_DIRECT_DISPATCH_BATCH_SIZE", 3), 3))


def _rows_from_result(result: Any) -> list[Any]:
    try:
        if isinstance(result, dict):
            rows = result.get("approved_rows")
            if isinstance(rows, list):
                return list(rows)
            rows = result.get("entries")
            if isinstance(rows, list):
                return list(rows)
            for key in ("result", "pipeline_result"):
                child = result.get(key)
                rows = _rows_from_result(child)
                if rows:
                    return rows
    except Exception:
        pass
    return []


def _build_rolling_rows_from_ai_results(ai_results: Any, existing_rows: list[Any]) -> list[Any]:
    """Build additional approved rows from AI_OK candidates. Final guards are not bypassed."""
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", True):
        return []
    try:
        from trading.entry.summary_ai import executor as ex
        existing_symbols = set(_symbols(existing_rows, limit=100))
        ok_items = [x for x in list(ai_results or []) if isinstance(x, dict) and bool(x.get("allow"))]
        try:
            kept = ex._filter_blocked_ai_ok_items(ok_items)
        except Exception:
            kept = ok_items
        try:
            ordered = sorted(kept, key=ex._sort_key, reverse=True)
        except Exception:
            ordered = kept
        scan_limit = max(_batch_size(), _env_int("SUMMARY_AI_DIRECT_DISPATCH_SCAN_LIMIT", 12))
        rows: list[Any] = []
        for item in ordered[:scan_limit]:
            sym = str(item.get("symbol") or "").strip()
            if not sym or sym in existing_symbols:
                continue
            try:
                row = ex.build_approved_row(item)
            except Exception:
                logger.debug("[SUMMARY AI DIRECT DISPATCH] build approved row failed symbol=%s", sym, exc_info=True)
                continue
            if row:
                rows.append(row)
                existing_symbols.add(sym)
        if rows:
            logger.warning(
                "[SUMMARY AI DIRECT DISPATCH] rolling extra approved rows built existing=%s extra=%s symbols=%s version=%s",
                _symbols(existing_rows, limit=100), len(rows), _symbols(rows, limit=100), VERSION,
            )
        return rows
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] rolling row build failed")
        return []


def _fallback_direct_dispatch(result: Any, kwargs: dict[str, Any], args: tuple[Any, ...] = ()) -> Any:
    try:
        if _result_executed(result):
            return result
        if not (_is_queued_async(result) or _is_retryable_no_order(result)):
            return result
        approved_rows = _rows_from_result(result)
        if not approved_rows:
            return result

        ai_results = kwargs.get("ai_results")
        if ai_results is None and args:
            ai_results = args[0]
        extra_rows = _build_rolling_rows_from_ai_results(ai_results, approved_rows)
        candidate_rows = list(approved_rows) + list(extra_rows)

        interval = kwargs.get("interval", 1)
        attempts = max(1, _env_int("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", 2))
        retry_sleep = max(0.3, _env_float("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", 0.7))
        timeout_sec = _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0)
        batch_n = _batch_size()
        last_result: Any = None
        attempt_records: list[dict[str, Any]] = []

        batches = [candidate_rows[i:i + batch_n] for i in range(0, len(candidate_rows), batch_n)]
        for batch_idx, batch in enumerate(batches, start=1):
            for attempt in range(1, attempts + 1):
                started = time.time()
                logger.warning(
                    "[SUMMARY AI DIRECT DISPATCH] rolling snapshot start batch=%s/%s attempt=%s/%s interval=%s approved=%s symbols=%s timeout=%.3fs price_floor=%.0f version=%s",
                    batch_idx, len(batches), attempt, attempts, interval, len(batch), _symbols(batch), timeout_sec, _summary_ai_price_floor(), VERSION,
                )
                snap_result = _call_with_timeout(
                    "direct_snapshot", batch, timeout_sec, lambda b=batch: _direct_snapshot_execute(b, interval)
                )
                last_result = snap_result
                executed = _result_executed(snap_result)
                timeout = _is_timeout_result(snap_result)
                retryable = _is_retryable_no_order(snap_result)
                attempt_records.append({
                    "batch": batch_idx,
                    "attempt": attempt,
                    "symbols": _symbols(batch),
                    "executed": executed,
                    "timeout": timeout,
                    "retryable": retryable,
                    "reason_chain": _flatten_reasons(snap_result),
                })
                logger.warning(
                    "[SUMMARY AI DIRECT DISPATCH] rolling snapshot done batch=%s/%s attempt=%s/%s elapsed=%.3fs executed=%s timeout=%s registered=%s retryable=%s reason_chain=%s result=%s",
                    batch_idx, len(batches), attempt, attempts, time.time() - started, executed, timeout,
                    _registered_count(snap_result), retryable, _flatten_reasons(snap_result), snap_result,
                )
                if executed or timeout or not retryable:
                    break
                if attempt < attempts:
                    time.sleep(retry_sleep)
            if _result_executed(last_result) or _is_timeout_result(last_result):
                break
            # no-orderでこのバッチが終わったら、次のAI_OK候補バッチへ進む。

        if isinstance(result, dict):
            out = dict(result)
            out["direct_dispatch_sync_fallback"] = True
            out["direct_dispatch_rolling"] = True
            out["direct_dispatch_attempts"] = attempt_records
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
    return _fallback_direct_dispatch(result, kwargs, args)


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
    global _INSTALLED, _ORIG
    try:
        _force_direct_sync_env()
        from trading.entry.summary_ai import executor as exec_mod
        from trading.entry.summary_ai import runner as runner_mod
        _install_executor_positive_result_patch(exec_mod)
        _install_executor_price_floor_patch(exec_mod)
        cur = getattr(exec_mod, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            logger.debug("[SUMMARY AI DIRECT DISPATCH] target missing")
            return False
        if getattr(cur, "_summary_ai_direct_dispatch_v11", False):
            _INSTALLED = True
            return True
        _ORIG = getattr(cur, "_original", cur) if any(getattr(cur, f"_summary_ai_direct_dispatch_v{i}", False) for i in range(1, 11)) else cur
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v11 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._summary_ai_direct_dispatch_v10 = True  # type: ignore[attr-defined]
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
                "[SUMMARY AI DIRECT DISPATCH] patched v11 target=%s direct_sync_env=%s attempts=%s snapshot_first=%s timeout=%.3fs strict_positive=%s price_floor=%.0f price_patch=%s rolling=%s scan=%s batch=%s source_match=True version=%s",
                getattr(_ORIG, "__name__", type(_ORIG).__name__),
                os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"),
                _env_int("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", 2),
                _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True),
                _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0),
                _POSITIVE_RESULT_PATCHED,
                _summary_ai_price_floor(),
                _PRICE_FLOOR_PATCHED,
                _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", True),
                _env_int("SUMMARY_AI_DIRECT_DISPATCH_SCAN_LIMIT", 12),
                _batch_size(),
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
                "[SUMMARY AI DIRECT DISPATCH] enforce v11 i=%s/%s ok=%s direct_sync_env=%s timeout=%.3fs strict_positive=%s price_floor=%.0f price_patch=%s rolling=%s version=%s",
                i, loops, ok, os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"),
                _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0), _POSITIVE_RESULT_PATCHED,
                _summary_ai_price_floor(), _PRICE_FLOOR_PATCHED, _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", True), VERSION,
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
            "[SUMMARY AI DIRECT DISPATCH] installed/enforcing v11 ok=%s watcher=%s loops=%s sleep=%s direct_sync_env=%s snapshot_first=%s timeout=%.3fs strict_positive=%s price_floor=%.0f price_patch=%s rolling=%s version=%s",
            ok,
            _WATCHER_STARTED,
            _env_int("SUMMARY_AI_DIRECT_DISPATCH_WATCH_LOOPS", 12),
            _env_float("SUMMARY_AI_DIRECT_DISPATCH_WATCH_SLEEP_SEC", 2.0),
            os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"),
            _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True),
            _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0),
            _POSITIVE_RESULT_PATCHED,
            _summary_ai_price_floor(),
            _PRICE_FLOOR_PATCHED,
            _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", True),
            VERSION,
        )
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI DIRECT DISPATCH] auto install failed")


__all__ = ["install", "VERSION"]
