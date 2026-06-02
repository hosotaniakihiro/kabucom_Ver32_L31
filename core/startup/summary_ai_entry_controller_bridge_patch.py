# ============================================================
# File   : core/startup/summary_ai_entry_controller_bridge_patch.py
# Version: V1.3-STRICT-ORDER-RESULT-BRIDGE
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が AI_OK → pending登録 → 数量計算まで進んだ後、
#   entry_controller の戻り値 None / 上位の approved 誤判定により
#   「実際に発注されない」「発注していないのに executed=True に見える」
#   問題を補正する。
#
# 方針:
#   1. SUMMARY_AI は既に summary_ai 側で AI_OK 済みなので、
#      entry_controller 内の再AI判定では既存 confidence/reason を再利用する。
#   2. entry_controller.run_entry_pipeline が None を返す旧仕様でも、
#      pending減少 / inflight増加 / 明示 order 情報から dict 結果へ正規化する。
#   3. lock競合時は短時間待機し、pending が残る場合は1回だけ再試行する。
#   4. summary_ai.executor / summary.pipeline.entry_pipeline の成功判定から
#      approved / registered だけの成功扱いを除外する。
#      実注文は executed=True / order_id / sent_orders / inflight 増加等だけで判定する。
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False

_ORDER_KEYS = ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols")
_EXECUTED_COUNT_KEYS = ("executed_count", "order_count", "submitted_count", "sent_count")


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return max(0.0, float(v))
    except Exception:
        return float(default)


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


def _norm_source(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _has_payload(v: Any) -> bool:
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) > 0
    return bool(v)


def _strict_order_executed(result: Any) -> bool:
    """実注文が確認できた場合だけ True。approved/registered は成功扱いしない。"""
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return bool(result)
        if isinstance(result, dict):
            if bool(result.get("executed")):
                return True
            for key in _EXECUTED_COUNT_KEYS:
                try:
                    if int(result.get(key) or 0) > 0:
                        return True
                except Exception:
                    pass
            for key in _ORDER_KEYS:
                if _has_payload(result.get(key)):
                    return True
            # ネスト結果も確認する。ただし approved / registered だけでは True にしない。
            for key in ("result", "pipeline_result", "order_result"):
                child = result.get(key)
                if child is not result and _strict_order_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_strict_order_executed(x) for x in result)
        return False
    except Exception:
        logger.exception("[SUMMARY AI ENTRY BRIDGE] strict order result judgement failed result=%s", result)
        return False


def _is_summary_ai_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    entry_type = _norm_source(row.get("entry_type"))
    source = _norm_source(row.get("source"))
    reason = _norm_source(row.get("reason") or row.get("ai_reason"))
    return entry_type == "SUMMARY_AI" or source == "SUMMARY" or "SRC=SUMMARY" in reason


def _is_summary_pipeline_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    try:
        src = kwargs.get("pipeline_source")
        if src is None and len(args) >= 1:
            src = args[0]
        return _norm_source(src) == "SUMMARY"
    except Exception:
        return False


def _summary_ai_result(row: dict) -> dict:
    confidence = max(
        _safe_float(row.get("ai_confidence"), 0.0),
        _safe_float(row.get("confidence"), 0.0),
        1.0,
    )
    lot_multiplier = max(_safe_float(row.get("lot_multiplier"), 1.0), 1.0)
    reason = str(row.get("ai_reason") or row.get("reason") or "SUMMARY_AI_PREAPPROVED")
    return {
        "allow": True,
        "confidence": confidence,
        "lot_multiplier": lot_multiplier,
        "reason": "SUMMARY_AI_PREAPPROVED|" + reason,
        "source": "SUMMARY_AI_PREAPPROVED_BRIDGE",
    }


def _count_pending(root: Any) -> int:
    try:
        if not isinstance(root, dict):
            return 0
        total = 0
        for v in root.values():
            try:
                if isinstance(v, (list, tuple, set, dict)):
                    total += len(v)
                elif v:
                    total += 1
            except Exception:
                pass
        return total
    except Exception:
        return 0


def _snapshot_pending_count(ec: Any) -> tuple[dict, int]:
    try:
        root = ec.snapshot_root()
        if isinstance(root, dict):
            return dict(root), _count_pending(root)
        return {}, 0
    except Exception:
        return {}, 0


def _inflight_count(ec: Any) -> int:
    try:
        gd = getattr(ec, "global_data", None)
        if gd is None:
            return 0
        for name in ("entry_inflight", "entry_inflight_orders", "inflight_entries", "inflight_orders"):
            v = getattr(gd, name, None)
            if isinstance(v, (dict, list, tuple, set)):
                return len(v)
        return 0
    except Exception:
        return 0


def _lock_is_held(ec: Any) -> bool:
    try:
        lock = getattr(ec, "_pipeline_lock", None)
        fn = getattr(lock, "locked", None)
        if callable(fn):
            return bool(fn())
    except Exception:
        pass
    return False


def _wait_entry_lock_if_needed(ec: Any, *, is_summary: bool, before_pending: int) -> tuple[bool, float]:
    if not is_summary or before_pending <= 0:
        return True, 0.0
    wait_sec = _env_float("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC", 8.0)
    poll_sec = _env_float("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC", 0.25)
    if wait_sec <= 0 or not _lock_is_held(ec):
        return True, 0.0

    started = time.time()
    logger.warning(
        "[SUMMARY AI ENTRY BRIDGE] entry_controller lock busy; wait start pending=%s wait_sec=%.3f poll=%.3f",
        before_pending,
        wait_sec,
        poll_sec,
    )
    while _lock_is_held(ec):
        elapsed = time.time() - started
        if elapsed >= wait_sec:
            logger.warning(
                "[SUMMARY AI ENTRY BRIDGE] entry_controller lock wait timeout elapsed=%.3fs pending=%s",
                elapsed,
                before_pending,
            )
            return False, elapsed
        time.sleep(max(0.05, poll_sec))
    elapsed = time.time() - started
    logger.warning("[SUMMARY AI ENTRY BRIDGE] entry_controller lock released; continue elapsed=%.3fs pending=%s", elapsed, before_pending)
    return True, elapsed


def _normalize_run_result(
    ec: Any,
    result: Any,
    *,
    before_root: dict,
    before_pending: int,
    before_inflight: int,
    waited_sec: float,
    is_summary: bool,
    retry_count: int,
) -> dict:
    after_root, after_pending = _snapshot_pending_count(ec)
    after_inflight = _inflight_count(ec)
    pending_decreased = after_pending < before_pending
    inflight_increased = after_inflight > before_inflight
    explicit_executed = _strict_order_executed(result)
    executed = bool(explicit_executed or pending_decreased or inflight_increased)
    approved_count = max(0, before_pending - after_pending, after_inflight - before_inflight)

    if isinstance(result, dict):
        out = dict(result)
    else:
        out = {"result": result}

    out.update(
        {
            "executed": executed,
            "approved_count": approved_count,
            "skip_reason": None if executed else (
                "entry_controller_no_order_after_retry" if retry_count > 0 else
                "entry_controller_no_order_after_lock_wait" if is_summary and waited_sec > 0 else
                "entry_controller_no_order"
            ),
            "pending_before": before_root,
            "pending_after": after_root,
            "pending_count_before": before_pending,
            "pending_count_after": after_pending,
            "inflight_before": before_inflight,
            "inflight_after": after_inflight,
            "waited_sec": waited_sec,
            "retry_count": retry_count,
        }
    )
    return out


def _should_retry_after_no_order(ec: Any, *, is_summary: bool, before_pending: int, before_inflight: int, result: Any) -> bool:
    if not _env_bool("SUMMARY_AI_ENTRY_CONTROLLER_RETRY_AFTER_SKIP", True):
        return False
    if not is_summary or before_pending <= 0:
        return False
    if _strict_order_executed(result):
        return False
    after_root, after_pending = _snapshot_pending_count(ec)
    after_inflight = _inflight_count(ec)
    return after_pending >= before_pending and after_inflight <= before_inflight and _count_pending(after_root) > 0


def _install_strict_result_judges() -> bool:
    ok = True
    try:
        import trading.entry.summary_ai.executor as executor

        old = getattr(executor, "_is_positive_order_result", None)
        if callable(old) and not getattr(old, "_summary_ai_strict_order_v13", False):
            def _patched_positive_order_result(result: Any) -> bool:
                return _strict_order_executed(result)

            _patched_positive_order_result._summary_ai_strict_order_v13 = True  # type: ignore[attr-defined]
            _patched_positive_order_result._original = old  # type: ignore[attr-defined]
            executor._is_positive_order_result = _patched_positive_order_result
            logger.warning("[SUMMARY AI ENTRY BRIDGE] summary_ai.executor strict order-result judge installed")
    except Exception:
        ok = False
        logger.exception("[SUMMARY AI ENTRY BRIDGE] executor strict judge install failed")

    try:
        import trading.summary.pipeline.entry_pipeline as ep

        old = getattr(ep, "_result_executed", None)
        if callable(old) and not getattr(old, "_summary_ai_strict_order_v13", False):
            def _patched_entry_pipeline_result(result: Any) -> bool:
                return _strict_order_executed(result)

            _patched_entry_pipeline_result._summary_ai_strict_order_v13 = True  # type: ignore[attr-defined]
            _patched_entry_pipeline_result._original = old  # type: ignore[attr-defined]
            ep._result_executed = _patched_entry_pipeline_result
            logger.warning("[SUMMARY AI ENTRY BRIDGE] summary.pipeline.entry_pipeline strict order-result judge installed")
    except Exception:
        ok = False
        logger.exception("[SUMMARY AI ENTRY BRIDGE] entry_pipeline strict judge install failed")

    return ok


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[SUMMARY AI ENTRY BRIDGE] entry_controller import failed")
        return False

    try:
        old_ai = getattr(ec, "ai_final_entry_check", None)
        if callable(old_ai) and not getattr(old_ai, "_summary_ai_preapproved_bridge", False):
            def _ai_final_entry_check_patched(entry_row: Any):
                try:
                    if isinstance(entry_row, dict) and _is_summary_ai_row(entry_row):
                        result = _summary_ai_result(entry_row)
                        ec.logger.info(
                            "[SUMMARY AI ENTRY BRIDGE] preapproved AI reused symbol=%s side=%s conf=%s score_buy=%s score_sell=%s source=%s interval=%s",
                            entry_row.get("symbol"),
                            entry_row.get("side") or entry_row.get("entry_decision"),
                            result.get("confidence"),
                            entry_row.get("score_buy", entry_row.get("buy_score")),
                            entry_row.get("score_sell", entry_row.get("sell_score")),
                            entry_row.get("source"),
                            entry_row.get("interval"),
                        )
                        return result
                except Exception:
                    logger.exception("[SUMMARY AI ENTRY BRIDGE] preapproved check failed; fallback original")
                return old_ai(entry_row)

            _ai_final_entry_check_patched._summary_ai_preapproved_bridge = True  # type: ignore[attr-defined]
            _ai_final_entry_check_patched._original_ai_final_entry_check = old_ai  # type: ignore[attr-defined]
            ec.ai_final_entry_check = _ai_final_entry_check_patched
            logger.warning("[SUMMARY AI ENTRY BRIDGE] ai_final_entry_check wrapper installed")
    except Exception:
        logger.exception("[SUMMARY AI ENTRY BRIDGE] ai wrapper install failed")
        return False

    try:
        old_run = getattr(ec, "run_entry_pipeline", None)
        if callable(old_run) and not getattr(old_run, "_summary_ai_return_bridge_v13", False):
            def _run_entry_pipeline_patched(*args, **kwargs):
                before_root, before_pending = _snapshot_pending_count(ec)
                before_inflight = _inflight_count(ec)
                is_summary = _is_summary_pipeline_call(args, kwargs)

                waited_ok, waited_sec = _wait_entry_lock_if_needed(ec, is_summary=is_summary, before_pending=before_pending)
                if not waited_ok:
                    out = {
                        "executed": False,
                        "approved_count": 0,
                        "result": None,
                        "skip_reason": "entry_controller_lock_timeout",
                        "pending_before": before_root,
                        "pending_after": before_root,
                        "pending_count_before": before_pending,
                        "pending_count_after": before_pending,
                        "inflight_before": before_inflight,
                        "inflight_after": before_inflight,
                        "waited_sec": waited_sec,
                        "retry_count": 0,
                    }
                    ec.logger.warning("[SUMMARY AI ENTRY BRIDGE] run_entry_pipeline blocked by lock timeout %s", out)
                    return out

                result = old_run(*args, **kwargs)
                retry_count = 0

                if _should_retry_after_no_order(ec, is_summary=is_summary, before_pending=before_pending, before_inflight=before_inflight, result=result):
                    retry_count = 1
                    retry_wait_ok, retry_waited = _wait_entry_lock_if_needed(ec, is_summary=True, before_pending=before_pending)
                    waited_sec += retry_waited
                    if retry_wait_ok:
                        ec.logger.warning(
                            "[SUMMARY AI ENTRY BRIDGE] retry run_entry_pipeline after no-order/lock-skip pending_before=%s waited_total=%.3fs",
                            before_pending,
                            waited_sec,
                        )
                        result = old_run(*args, **kwargs)
                    else:
                        ec.logger.warning(
                            "[SUMMARY AI ENTRY BRIDGE] retry skipped because lock timeout pending_before=%s waited_total=%.3fs",
                            before_pending,
                            waited_sec,
                        )

                out = _normalize_run_result(
                    ec,
                    result,
                    before_root=before_root,
                    before_pending=before_pending,
                    before_inflight=before_inflight,
                    waited_sec=waited_sec,
                    is_summary=is_summary,
                    retry_count=retry_count,
                )
                ec.logger.info("[SUMMARY AI ENTRY BRIDGE] run_entry_pipeline return normalized %s", out)
                return out

            _run_entry_pipeline_patched._summary_ai_return_bridge_v13 = True  # type: ignore[attr-defined]
            _run_entry_pipeline_patched._summary_ai_return_bridge_v12 = True  # type: ignore[attr-defined]
            _run_entry_pipeline_patched._summary_ai_return_bridge_v11 = True  # type: ignore[attr-defined]
            _run_entry_pipeline_patched._summary_ai_return_bridge = True  # type: ignore[attr-defined]
            _run_entry_pipeline_patched._original_run_entry_pipeline = old_run  # type: ignore[attr-defined]
            ec.run_entry_pipeline = _run_entry_pipeline_patched
            logger.warning(
                "[SUMMARY AI ENTRY BRIDGE] run_entry_pipeline return/lock-wait/retry wrapper installed wait_sec=%.3f poll=%.3f retry=%s",
                _env_float("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC", 8.0),
                _env_float("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC", 0.25),
                _env_bool("SUMMARY_AI_ENTRY_CONTROLLER_RETRY_AFTER_SKIP", True),
            )
    except Exception:
        logger.exception("[SUMMARY AI ENTRY BRIDGE] run wrapper install failed")
        return False

    strict_ok = _install_strict_result_judges()

    _PATCHED = True
    logger.warning("[SUMMARY AI ENTRY BRIDGE] installed v1.3 strict_ok=%s", strict_ok)
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ENTRY BRIDGE] auto install failed")


__all__ = ["install"]
