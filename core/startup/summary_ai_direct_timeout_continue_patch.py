# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_direct_timeout_continue_patch.py
# Version: V15-DIRECT-DISPATCH-CONTINUE-NEXT-FRESH-CANDIDATE
# ------------------------------------------------------------
# Purpose:
#   SUMMARY AI direct_snapshot の timeout を、候補3銘柄まとめ投げで
#   全滅させない。ただし、AI_OK から時間が経った候補を
#   遅れて発注しない。
#
# V15:
#   - direct snapshot は必ず1銘柄ずつ実行する。
#   - timeoutした銘柄だけをスキップし、fresh window 内なら次のAI_OK候補へ進む。
#   - timeoutした内側スレッドが遅れて pending 登録へ進んでも、
#     register_pending_entries 直前で期限切れなら拒否する。
#   - 低出来高・低変動・blowoff・板ガードは緩めない。
# ============================================================
from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V15-DIRECT-DISPATCH-CONTINUE-NEXT-FRESH-CANDIDATE"
_INSTALLED = False
_PENDING_GUARD_INSTALLED = False

# symbol -> {valid_until_ts, approved_at_ts, max_age_sec}
_FRESHNESS_BY_SYMBOL: dict[str, dict[str, float]] = {}

_NO_RETRY_SAME_ROW_MARKERS = (
    "entry_pipeline_no_order",
    "no_tradable_rows_after_filters",
    "blowoff",
    "liquidity",
    "sell_credit",
    "position",
    "low_move",
    "range_atr",
)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _should_skip_original_rows(direct: Any, result: Any) -> bool:
    try:
        reason = str(direct._flatten_reasons(result) or "").lower()
        return any(x in reason for x in _NO_RETRY_SAME_ROW_MARKERS)
    except Exception:
        return False


def _dedupe_rows(direct: Any, rows: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    try:
        for r in list(rows or []):
            sym = ""
            try:
                sym = str(direct._pick_symbol(r) or "").strip()
            except Exception:
                sym = ""
            key = sym or str(id(r))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    except Exception:
        return list(rows or [])
    return out


def _row_to_mutable_dict(row: Any) -> dict[str, Any] | None:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        return None
    return None


def _force_direct_one_by_one_env(direct: Any | None = None) -> None:
    # This is not a guard relaxation. It only isolates slow board/snapshot calls per symbol.
    os.environ["SUMMARY_AI_DIRECT_DISPATCH_BATCH_SIZE"] = "1"
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SCAN_LIMIT", "12")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", "0.3")
    os.environ.setdefault("SUMMARY_AI_DIRECT_FRESH_MAX_ATTEMPTS", "1")
    os.environ.setdefault("SUMMARY_AI_OK_VALID_SEC", "12.0")
    os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_FRESH_TIMEOUT_SEC", "2.0")
    if direct is not None:
        try:
            def _batch_size_one() -> int:
                return 1
            _batch_size_one._summary_ai_direct_one_by_one_v15 = True  # type: ignore[attr-defined]
            direct._batch_size = _batch_size_one
        except Exception:
            pass


def _mark_rows_fresh(direct: Any, rows: list[Any], *, approved_at_ts: float, max_age_sec: float) -> list[Any]:
    out: list[Any] = []
    valid_until_ts = float(approved_at_ts) + max(0.1, float(max_age_sec or 0.0))
    try:
        now = time.time()
        for sym, meta in list(_FRESHNESS_BY_SYMBOL.items()):
            try:
                if float(meta.get("valid_until_ts") or 0.0) < now - 30.0:
                    _FRESHNESS_BY_SYMBOL.pop(sym, None)
            except Exception:
                _FRESHNESS_BY_SYMBOL.pop(sym, None)

        for row in list(rows or []):
            sym = ""
            try:
                sym = str(direct._pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            if sym:
                _FRESHNESS_BY_SYMBOL[sym] = {
                    "approved_at_ts": float(approved_at_ts),
                    "valid_until_ts": float(valid_until_ts),
                    "max_age_sec": float(max_age_sec),
                }
            d = _row_to_mutable_dict(row)
            if d is not None:
                d.setdefault("summary_ai_approved_at_ts", float(approved_at_ts))
                d.setdefault("summary_ai_valid_until_ts", float(valid_until_ts))
                d.setdefault("summary_ai_max_age_sec", float(max_age_sec))
                d.setdefault("summary_ai_fresh_guard_version", VERSION)
            out.append(row)
    except Exception:
        logger.debug("[SUMMARY AI DIRECT FRESHNESS] mark rows failed", exc_info=True)
        return list(rows or [])
    return out


def _is_row_fresh(direct: Any, row: Any, *, now_ts: float | None = None) -> bool:
    try:
        now = float(now_ts if now_ts is not None else time.time())
        d = _row_to_mutable_dict(row) or {}
        valid_until = d.get("summary_ai_valid_until_ts")
        if valid_until is None:
            sym = ""
            try:
                sym = str(direct._pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            meta = _FRESHNESS_BY_SYMBOL.get(sym) if sym else None
            if meta:
                valid_until = meta.get("valid_until_ts")
        if valid_until is None:
            return True
        return now <= float(valid_until)
    except Exception:
        return True


def _filter_fresh_rows(direct: Any, rows: list[Any]) -> tuple[list[Any], list[str]]:
    fresh: list[Any] = []
    stale_symbols: list[str] = []
    now = time.time()
    for row in list(rows or []):
        if _is_row_fresh(direct, row, now_ts=now):
            fresh.append(row)
        else:
            try:
                sym = str(direct._pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            stale_symbols.append(sym or "?")
    return fresh, stale_symbols


def _install_pending_freshness_guard() -> bool:
    """timeoutした内側スレッドが遅れて pending 登録しても、期限切れなら拒否する。"""
    global _PENDING_GUARD_INSTALLED
    if _PENDING_GUARD_INSTALLED:
        return True
    try:
        from trading.summary import summary_entry as se

        cur = getattr(se, "register_pending_entries", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_fresh_pending_guard_v15", False):
            _PENDING_GUARD_INSTALLED = True
            return True

        original_register = getattr(cur, "_original", cur)

        @functools.wraps(original_register)
        def _register_pending_entries_fresh_guard(entries: list[dict[str, Any]], *args: Any, **kwargs: Any) -> int:
            try:
                if not entries:
                    return original_register(entries, *args, **kwargs)
                now = time.time()
                kept: list[dict[str, Any]] = []
                skipped: list[str] = []
                for entry in list(entries or []):
                    try:
                        if not isinstance(entry, dict):
                            kept.append(entry)
                            continue
                        sym = str(entry.get("symbol") or "").strip()
                        meta = _FRESHNESS_BY_SYMBOL.get(sym) if sym else None
                        valid_until = entry.get("summary_ai_valid_until_ts")
                        if valid_until is None and meta:
                            valid_until = meta.get("valid_until_ts")
                        if valid_until is not None and now > float(valid_until):
                            skipped.append(sym or "?")
                            continue
                        kept.append(entry)
                    except Exception:
                        kept.append(entry)
                if skipped:
                    logger.warning(
                        "[SUMMARY AI DIRECT FRESHNESS] pending skip expired symbols=%s kept=%s skipped=%s version=%s",
                        skipped,
                        len(kept),
                        len(skipped),
                        VERSION,
                    )
                if not kept:
                    return 0
                return original_register(kept, *args, **kwargs)
            except Exception:
                logger.exception("[SUMMARY AI DIRECT FRESHNESS] pending guard failed; use original")
                return original_register(entries, *args, **kwargs)

        _register_pending_entries_fresh_guard._summary_ai_fresh_pending_guard_v15 = True  # type: ignore[attr-defined]
        _register_pending_entries_fresh_guard._summary_ai_fresh_pending_guard_v14 = True  # type: ignore[attr-defined]
        _register_pending_entries_fresh_guard._original = original_register  # type: ignore[attr-defined]
        se.register_pending_entries = _register_pending_entries_fresh_guard
        _PENDING_GUARD_INSTALLED = True
        logger.warning("[SUMMARY AI DIRECT FRESHNESS] pending registration expiry guard installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT FRESHNESS] pending guard install failed")
        return False


def _install_native_direct_guards(direct: Any) -> bool:
    try:
        _force_direct_one_by_one_env(direct)
        orig_force_env = getattr(direct, "_force_direct_sync_env", None)
        if callable(orig_force_env) and not getattr(orig_force_env, "_summary_ai_direct_one_by_one_v15", False):
            @functools.wraps(orig_force_env)
            def _force_env_one_by_one() -> None:
                try:
                    orig_force_env()
                finally:
                    _force_direct_one_by_one_env(direct)

            _force_env_one_by_one._summary_ai_direct_one_by_one_v15 = True  # type: ignore[attr-defined]
            _force_env_one_by_one._original = orig_force_env  # type: ignore[attr-defined]
            direct._force_direct_sync_env = _force_env_one_by_one
        logger.warning(
            "[SUMMARY AI DIRECT TIMEOUT CONTINUE] native one-by-one enforced timeout=%.3fs valid_sec=%.3fs version=%s direct_version=%s",
            direct._env_float("SUMMARY_AI_DIRECT_SNAPSHOT_FRESH_TIMEOUT_SEC", 2.0),
            direct._env_float("SUMMARY_AI_OK_VALID_SEC", 12.0),
            VERSION,
            getattr(direct, "VERSION", "unknown"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] native direct guard install failed")
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        _install_pending_freshness_guard()
        return True
    try:
        from core.startup import summary_ai_async_direct_dispatch_patch as direct

        native_ok = _install_native_direct_guards(direct)
        cur = getattr(direct, "_fallback_direct_dispatch", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI DIRECT TIMEOUT CONTINUE] target not callable native_ok=%s version=%s", native_ok, VERSION)
            return bool(native_ok)
        if getattr(cur, "_summary_ai_direct_timeout_continue_v15", False):
            _INSTALLED = True
            _install_pending_freshness_guard()
            return True

        original = getattr(cur, "_original", cur)

        @functools.wraps(original)
        def _fallback_direct_dispatch_timeout_continue(result: Any, kwargs: dict[str, Any], args: tuple[Any, ...] = ()) -> Any:
            _force_direct_one_by_one_env(direct)
            try:
                if direct._result_executed(result):
                    return result
                if not (direct._is_queued_async(result) or direct._is_retryable_no_order(result)):
                    return result

                approved_rows = direct._rows_from_result(result)
                if not approved_rows:
                    return result

                ai_results = kwargs.get("ai_results")
                if ai_results is None and args:
                    ai_results = args[0]

                extra_rows = direct._build_rolling_rows_from_ai_results(ai_results, approved_rows)
                skip_original = _should_skip_original_rows(direct, result)
                candidate_rows = list(extra_rows) if skip_original else (list(approved_rows) + list(extra_rows))
                candidate_rows = _dedupe_rows(direct, candidate_rows)
                if not candidate_rows:
                    return result

                approved_at_ts = time.time()
                max_age_sec = max(1.0, direct._env_float("SUMMARY_AI_OK_VALID_SEC", 12.0))
                candidate_rows = _mark_rows_fresh(direct, candidate_rows, approved_at_ts=approved_at_ts, max_age_sec=max_age_sec)
                candidate_rows, stale_symbols = _filter_fresh_rows(direct, candidate_rows)
                if not candidate_rows:
                    if isinstance(result, dict):
                        out = dict(result)
                        out["executed"] = False
                        out["skip_reason"] = "summary_ai_ok_expired_before_dispatch"
                        out["summary_ai_expired_symbols"] = stale_symbols
                        out["summary_ai_ok_valid_sec"] = max_age_sec
                        return out
                    return result

                interval = kwargs.get("interval", 1)
                attempts = max(1, direct._env_int("SUMMARY_AI_DIRECT_FRESH_MAX_ATTEMPTS", 1))
                retry_sleep = max(0.1, direct._env_float("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", 0.3))
                configured_timeout = direct._env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0)
                fresh_timeout = direct._env_float("SUMMARY_AI_DIRECT_SNAPSHOT_FRESH_TIMEOUT_SEC", 2.0)
                timeout_sec = max(0.3, min(float(configured_timeout), float(fresh_timeout), float(max_age_sec)))

                batches = [[r] for r in candidate_rows]
                last_result: Any = None
                timeout_seen = False
                freshness_expired_seen = False
                attempt_records: list[dict[str, Any]] = []

                for batch_idx, batch in enumerate(batches, start=1):
                    fresh_batch, stale_batch_symbols = _filter_fresh_rows(direct, batch)
                    if stale_batch_symbols:
                        freshness_expired_seen = True
                        attempt_records.append({
                            "batch": batch_idx,
                            "attempt": 0,
                            "symbols": stale_batch_symbols,
                            "executed": False,
                            "timeout": False,
                            "retryable": False,
                            "reason_chain": "summary_ai_ok_expired_before_snapshot",
                        })
                    if not fresh_batch:
                        continue

                    for attempt in range(1, attempts + 1):
                        if time.time() > approved_at_ts + max_age_sec:
                            freshness_expired_seen = True
                            logger.warning(
                                "[SUMMARY AI DIRECT FRESHNESS] stop dispatch expired elapsed=%.3fs max_age=%.3fs remaining_symbols=%s version=%s",
                                time.time() - approved_at_ts,
                                max_age_sec,
                                direct._symbols(fresh_batch),
                                VERSION,
                            )
                            break

                        started = time.time()
                        logger.warning(
                            "[SUMMARY AI DIRECT DISPATCH] fresh one-by-one snapshot start batch=%s/%s attempt=%s/%s interval=%s approved=%s symbols=%s timeout=%.3fs valid_sec=%.3fs price_floor=%.0f skip_original=%s version=%s timeout_continue=%s",
                            batch_idx,
                            len(batches),
                            attempt,
                            attempts,
                            interval,
                            len(fresh_batch),
                            direct._symbols(fresh_batch),
                            timeout_sec,
                            max_age_sec,
                            direct._summary_ai_price_floor(),
                            skip_original,
                            getattr(direct, "VERSION", "unknown"),
                            VERSION,
                        )
                        snap_result = direct._call_with_timeout(
                            "direct_snapshot",
                            fresh_batch,
                            timeout_sec,
                            lambda b=fresh_batch: direct._direct_snapshot_execute(b, interval),
                        )
                        last_result = snap_result
                        executed = direct._result_executed(snap_result)
                        timeout = direct._is_timeout_result(snap_result)
                        retryable = direct._is_retryable_no_order(snap_result)
                        timeout_seen = bool(timeout_seen or timeout)
                        attempt_records.append({
                            "batch": batch_idx,
                            "attempt": attempt,
                            "symbols": direct._symbols(fresh_batch),
                            "executed": executed,
                            "timeout": timeout,
                            "retryable": retryable,
                            "reason_chain": direct._flatten_reasons(snap_result),
                        })
                        logger.warning(
                            "[SUMMARY AI DIRECT DISPATCH] fresh one-by-one snapshot done batch=%s/%s attempt=%s/%s elapsed=%.3fs executed=%s timeout=%s registered=%s retryable=%s reason_chain=%s result=%s timeout_continue=%s",
                            batch_idx,
                            len(batches),
                            attempt,
                            attempts,
                            time.time() - started,
                            executed,
                            timeout,
                            direct._registered_count(snap_result),
                            retryable,
                            direct._flatten_reasons(snap_result),
                            snap_result,
                            VERSION,
                        )
                        if executed:
                            break
                        if timeout:
                            # V15: timeoutした銘柄だけ捨てる。fresh window内なら次のAI_OK候補へ進む。
                            logger.warning(
                                "[SUMMARY AI DIRECT DISPATCH] timeout skip symbol and continue symbols=%s timeout=%.3fs valid_sec=%.3fs version=%s",
                                direct._symbols(fresh_batch),
                                timeout_sec,
                                max_age_sec,
                                VERSION,
                            )
                            break
                        if not retryable:
                            break
                        if attempt < attempts:
                            time.sleep(retry_sleep)

                    if direct._result_executed(last_result):
                        break
                    # timeout / no-order / hard-filter NG でも次の1銘柄候補へ進む。

                if isinstance(result, dict):
                    out = dict(result)
                    out["direct_dispatch_sync_fallback"] = True
                    out["direct_dispatch_rolling"] = True
                    out["direct_dispatch_timeout_continue"] = True
                    out["direct_dispatch_one_by_one"] = True
                    out["direct_dispatch_fresh_guard"] = True
                    out["direct_dispatch_skip_original_failed_rows"] = bool(skip_original)
                    out["direct_dispatch_timeout_seen"] = bool(timeout_seen)
                    out["direct_dispatch_freshness_expired_seen"] = bool(freshness_expired_seen)
                    out["direct_dispatch_attempts"] = attempt_records
                    out["direct_dispatch_result"] = last_result
                    out["summary_ai_ok_valid_sec"] = max_age_sec
                    out["summary_ai_direct_snapshot_timeout_sec"] = timeout_sec
                    if direct._result_executed(last_result):
                        out["executed"] = True
                        out["skip_reason"] = None
                    else:
                        out["executed"] = False
                        if bool(freshness_expired_seen):
                            out["skip_reason"] = "summary_ai_ok_expired_before_dispatch"
                        elif bool(timeout_seen):
                            out["skip_reason"] = "direct_snapshot_timeout_after_candidates"
                            out["direct_dispatch_timeout"] = True
                        else:
                            out["skip_reason"] = direct._flatten_reasons(last_result) or "entry_pipeline_no_order"
                    return out
            except Exception:
                logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] patched fallback failed; use original")
            return original(result, kwargs, args)

        _fallback_direct_dispatch_timeout_continue._summary_ai_direct_timeout_continue_v15 = True  # type: ignore[attr-defined]
        _fallback_direct_dispatch_timeout_continue._summary_ai_direct_timeout_continue_v14 = True  # type: ignore[attr-defined]
        _fallback_direct_dispatch_timeout_continue._summary_ai_direct_timeout_continue_v13 = True  # type: ignore[attr-defined]
        _fallback_direct_dispatch_timeout_continue._summary_ai_direct_timeout_continue_v12 = True  # type: ignore[attr-defined]
        _fallback_direct_dispatch_timeout_continue._original = original  # type: ignore[attr-defined]
        direct._fallback_direct_dispatch = _fallback_direct_dispatch_timeout_continue
        _install_pending_freshness_guard()
        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI DIRECT TIMEOUT CONTINUE] installed version=%s direct_version=%s fresh_valid_sec=%.3f fresh_timeout=%.3f pending_guard=%s one_by_one=True continue_after_timeout=True",
            VERSION,
            getattr(direct, "VERSION", "unknown"),
            direct._env_float("SUMMARY_AI_OK_VALID_SEC", 12.0),
            direct._env_float("SUMMARY_AI_DIRECT_SNAPSHOT_FRESH_TIMEOUT_SEC", 2.0),
            _PENDING_GUARD_INSTALLED,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] auto install failed")


__all__ = ["install", "VERSION"]
