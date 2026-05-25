# ============================================================
# File   : core/startup/summary_ai_entry_controller_bridge_patch.py
# Version: V1.1-WAIT-ENTRY-PIPELINE-LOCK
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が AI_OK を出して pending 登録まで成功しているのに、
#   entry_controller 側で再AI判定/戻り値None/実行中ロックにより
#   「APPROVED=0」「entry_controller_no_order」「ENTRY PIPELINE already running」
#   になりやすい問題を補正する。
#
# 方針:
#   1. SUMMARY_AI の pending 候補は、既に summary_ai 側で AI_OK 済みなので、
#      entry_controller 内の ai_final_entry_check 再判定では既存の confidence/reason を使う。
#   2. entry_controller.run_entry_pipeline が None を返す旧仕様を、
#      summary_entry 側で判定できる dict 戻り値へ補正する。
#   3. entry_controller の _pipeline_lock が他ルートで使用中の場合、
#      SUMMARY_AI を即 skip せず、短時間待ってから実行する。
#   4. 実際の安全ガードは維持する。
#      - market/risk/index shock/position/sell credit/ATR/range/order builder/qty はそのまま通す。
#
# ENV:
#   SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC=8.0
#   SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC=0.25
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        return x
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


def _norm_source(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_summary_ai_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    entry_type = _norm_source(row.get("entry_type"))
    source = _norm_source(row.get("source"))
    reason = _norm_source(row.get("reason") or row.get("ai_reason"))
    return (
        entry_type == "SUMMARY_AI"
        or source == "SUMMARY"
        or "SRC=SUMMARY" in reason
    )


def _is_summary_pipeline_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    try:
        src = kwargs.get("pipeline_source")
        if src is None and len(args) >= 1:
            # 旧互換。通常 run_entry_pipeline は keyword only だが保険。
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
    lot_multiplier = max(
        _safe_float(row.get("lot_multiplier"), 1.0),
        1.0,
    )
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
    """
    SUMMARY_AI が pending 登録済みなのに entry_controller が別ルート実行中なら待つ。
    非SUMMARYや pending 無しは従来通り待たない。
    """
    if not is_summary or before_pending <= 0:
        return True, 0.0

    wait_sec = _env_float("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC", 8.0)
    poll_sec = _env_float("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC", 0.25)
    if wait_sec <= 0:
        return True, 0.0

    started = time.time()
    if not _lock_is_held(ec):
        return True, 0.0

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
    logger.warning(
        "[SUMMARY AI ENTRY BRIDGE] entry_controller lock released; continue elapsed=%.3fs pending=%s",
        elapsed,
        before_pending,
    )
    return True, elapsed


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[SUMMARY AI ENTRY BRIDGE] entry_controller import failed")
        return False

    # --------------------------------------------------------
    # 1) SUMMARY_AI は summary_ai 側でAI_OK済みなので、
    #    entry_controller の再AI判定は preapproved として扱う。
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # 2) entry_controller.run_entry_pipeline の戻り値を dict 化。
    #    かつ SUMMARY_AI の場合は _pipeline_lock が空くまで短時間待つ。
    # --------------------------------------------------------
    try:
        old_run = getattr(ec, "run_entry_pipeline", None)
        if callable(old_run) and not getattr(old_run, "_summary_ai_return_bridge_v11", False):
            def _run_entry_pipeline_patched(*args, **kwargs):
                before_root, before_pending = _snapshot_pending_count(ec)
                before_inflight = _inflight_count(ec)
                is_summary = _is_summary_pipeline_call(args, kwargs)

                waited_ok, waited_sec = _wait_entry_lock_if_needed(
                    ec,
                    is_summary=is_summary,
                    before_pending=before_pending,
                )
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
                    }
                    ec.logger.warning("[SUMMARY AI ENTRY BRIDGE] run_entry_pipeline blocked by lock timeout %s", out)
                    return out

                result = old_run(*args, **kwargs)

                if isinstance(result, dict):
                    result.setdefault("waited_sec", waited_sec)
                    return result
                if isinstance(result, bool):
                    return {
                        "executed": bool(result),
                        "approved_count": 1 if result else 0,
                        "result": result,
                        "skip_reason": None if result else "entry_controller_no_order",
                        "waited_sec": waited_sec,
                    }

                after_root, after_pending = _snapshot_pending_count(ec)
                after_inflight = _inflight_count(ec)
                pending_decreased = after_pending < before_pending
                inflight_increased = after_inflight > before_inflight
                executed = bool(pending_decreased or inflight_increased)
                approved_count = max(0, before_pending - after_pending, after_inflight - before_inflight)

                # ロック待ちをしたのに pending が残ったままなら、原因が見えるようにする。
                skip_reason = None if executed else "entry_controller_no_order"
                if not executed and is_summary and waited_sec > 0:
                    skip_reason = "entry_controller_no_order_after_lock_wait"

                out = {
                    "executed": executed,
                    "approved_count": approved_count,
                    "result": result,
                    "skip_reason": skip_reason,
                    "pending_before": before_root,
                    "pending_after": after_root,
                    "pending_count_before": before_pending,
                    "pending_count_after": after_pending,
                    "inflight_before": before_inflight,
                    "inflight_after": after_inflight,
                    "waited_sec": waited_sec,
                }
                ec.logger.info("[SUMMARY AI ENTRY BRIDGE] run_entry_pipeline return normalized %s", out)
                return out

            _run_entry_pipeline_patched._summary_ai_return_bridge_v11 = True  # type: ignore[attr-defined]
            _run_entry_pipeline_patched._summary_ai_return_bridge = True  # type: ignore[attr-defined]
            _run_entry_pipeline_patched._original_run_entry_pipeline = old_run  # type: ignore[attr-defined]
            ec.run_entry_pipeline = _run_entry_pipeline_patched
            logger.warning(
                "[SUMMARY AI ENTRY BRIDGE] run_entry_pipeline return/lock-wait wrapper installed wait_sec=%.3f poll=%.3f",
                _env_float("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_WAIT_SEC", 8.0),
                _env_float("SUMMARY_AI_ENTRY_CONTROLLER_LOCK_POLL_SEC", 0.25),
            )
    except Exception:
        logger.exception("[SUMMARY AI ENTRY BRIDGE] run wrapper install failed")
        return False

    _PATCHED = True
    logger.warning("[SUMMARY AI ENTRY BRIDGE] installed v1.1")
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ENTRY BRIDGE] auto install failed")


__all__ = ["install"]
