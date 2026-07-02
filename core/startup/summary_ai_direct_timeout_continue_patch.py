# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_direct_timeout_continue_patch.py
# Version: V13-DIRECT-DISPATCH-ONE-BY-ONE-SKIP-FAILED
# ------------------------------------------------------------
# Purpose:
#   SUMMARY AI direct snapshot の timeout を、候補3銘柄まとめ投げで
#   全滅させない。
#
# V13:
#   - 直前に entry_pipeline_no_order / no_tradable_rows_after_filters で
#     落ちた approved_rows を同じ fallback 内で再投入しない。
#   - rolling AI_OK 候補を 1銘柄ずつ direct_snapshot に通す。
#   - 低出来高・低変動・blowoff・板ガードは緩めない。
# ============================================================
from __future__ import annotations

import functools
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V13-DIRECT-DISPATCH-ONE-BY-ONE-SKIP-FAILED"
_INSTALLED = False

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


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from core.startup import summary_ai_async_direct_dispatch_patch as direct

        cur = getattr(direct, "_fallback_direct_dispatch", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI DIRECT TIMEOUT CONTINUE] target not callable version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_direct_timeout_continue_v13", False):
            _INSTALLED = True
            return True

        original = getattr(cur, "_original", cur)

        @functools.wraps(original)
        def _fallback_direct_dispatch_timeout_continue(result: Any, kwargs: dict[str, Any], args: tuple[Any, ...] = ()) -> Any:
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

                interval = kwargs.get("interval", 1)
                attempts = max(1, direct._env_int("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", 2))
                retry_sleep = max(0.3, direct._env_float("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", 0.7))
                timeout_sec = direct._env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0)

                # V13: direct snapshot は1銘柄ずつ。遅い/詰まる銘柄で他のAI_OK候補を巻き込まない。
                batches = [[r] for r in candidate_rows]
                last_result: Any = None
                timeout_seen = False
                attempt_records: list[dict[str, Any]] = []

                for batch_idx, batch in enumerate(batches, start=1):
                    for attempt in range(1, attempts + 1):
                        started = time.time()
                        logger.warning(
                            "[SUMMARY AI DIRECT DISPATCH] rolling snapshot start batch=%s/%s attempt=%s/%s interval=%s approved=%s symbols=%s timeout=%.3fs price_floor=%.0f skip_original=%s one_by_one=True version=%s timeout_continue=%s",
                            batch_idx,
                            len(batches),
                            attempt,
                            attempts,
                            interval,
                            len(batch),
                            direct._symbols(batch),
                            timeout_sec,
                            direct._summary_ai_price_floor(),
                            skip_original,
                            getattr(direct, "VERSION", "unknown"),
                            VERSION,
                        )
                        snap_result = direct._call_with_timeout(
                            "direct_snapshot",
                            batch,
                            timeout_sec,
                            lambda b=batch: direct._direct_snapshot_execute(b, interval),
                        )
                        last_result = snap_result
                        executed = direct._result_executed(snap_result)
                        timeout = direct._is_timeout_result(snap_result)
                        retryable = direct._is_retryable_no_order(snap_result)
                        timeout_seen = bool(timeout_seen or timeout)
                        attempt_records.append({
                            "batch": batch_idx,
                            "attempt": attempt,
                            "symbols": direct._symbols(batch),
                            "executed": executed,
                            "timeout": timeout,
                            "retryable": retryable,
                            "reason_chain": direct._flatten_reasons(snap_result),
                        })
                        logger.warning(
                            "[SUMMARY AI DIRECT DISPATCH] rolling snapshot done batch=%s/%s attempt=%s/%s elapsed=%.3fs executed=%s timeout=%s registered=%s retryable=%s reason_chain=%s result=%s timeout_continue=%s",
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
                            # 同じ遅い銘柄を再試行せず、次のAI_OK候補へ進める。
                            logger.warning(
                                "[SUMMARY AI DIRECT DISPATCH] timeout continue next symbol batch=%s/%s attempt=%s/%s symbols=%s version=%s",
                                batch_idx,
                                len(batches),
                                attempt,
                                attempts,
                                direct._symbols(batch),
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
                    out["direct_dispatch_skip_original_failed_rows"] = bool(skip_original)
                    out["direct_dispatch_timeout_seen"] = bool(timeout_seen)
                    out["direct_dispatch_attempts"] = attempt_records
                    out["direct_dispatch_result"] = last_result
                    if direct._result_executed(last_result):
                        out["executed"] = True
                        out["skip_reason"] = None
                    else:
                        out["executed"] = False
                        if bool(timeout_seen):
                            out["skip_reason"] = "direct_snapshot_timeout_after_candidates"
                            out["direct_dispatch_timeout"] = True
                        else:
                            out["skip_reason"] = direct._flatten_reasons(last_result) or "entry_pipeline_no_order"
                    return out
            except Exception:
                logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] patched fallback failed; use original")
            return original(result, kwargs, args)

        _fallback_direct_dispatch_timeout_continue._summary_ai_direct_timeout_continue_v13 = True  # type: ignore[attr-defined]
        _fallback_direct_dispatch_timeout_continue._summary_ai_direct_timeout_continue_v12 = True  # type: ignore[attr-defined]
        _fallback_direct_dispatch_timeout_continue._original = original  # type: ignore[attr-defined]
        direct._fallback_direct_dispatch = _fallback_direct_dispatch_timeout_continue
        _INSTALLED = True
        logger.warning("[SUMMARY AI DIRECT TIMEOUT CONTINUE] installed version=%s direct_version=%s", VERSION, getattr(direct, "VERSION", "unknown"))
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] auto install failed")


__all__ = ["install", "VERSION"]
