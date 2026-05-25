# ============================================================
# File   : core/startup/summary_ai_entry_controller_bridge_patch.py
# Version: V1.0-SUMMARY-AI-PREAPPROVED-BRIDGE
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が AI_OK を出して pending 登録まで成功しているのに、
#   entry_controller 側で再AI判定/戻り値Noneにより
#   「APPROVED=0」「entry_controller_no_order」になりやすい問題を補正する。
#
# 方針:
#   1. SUMMARY_AI の pending 候補は、既に summary_ai 側で AI_OK 済みなので、
#      entry_controller 内の ai_final_entry_check 再判定では既存の confidence/reason を使う。
#   2. entry_controller.run_entry_pipeline が None を返す旧仕様を、
#      summary_entry 側で判定できる dict 戻り値へ補正する。
#   3. 実際の安全ガードは維持する。
#      - market/risk/index shock/position/sell credit/ATR/range/order builder/qty はそのまま通す。
# ============================================================

from __future__ import annotations

import logging
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
    #    旧実装は内部で注文しても None なので、summary_entry 側が
    #    entry_controller_no_order と誤判定しやすい。
    # --------------------------------------------------------
    try:
        old_run = getattr(ec, "run_entry_pipeline", None)
        if callable(old_run) and not getattr(old_run, "_summary_ai_return_bridge", False):
            def _run_entry_pipeline_patched(*args, **kwargs):
                before_root, before_pending = _snapshot_pending_count(ec)
                before_inflight = _inflight_count(ec)
                result = old_run(*args, **kwargs)

                if isinstance(result, dict):
                    return result
                if isinstance(result, bool):
                    return {
                        "executed": bool(result),
                        "approved_count": 1 if result else 0,
                        "result": result,
                        "skip_reason": None if result else "entry_controller_no_order",
                    }

                after_root, after_pending = _snapshot_pending_count(ec)
                after_inflight = _inflight_count(ec)
                pending_decreased = after_pending < before_pending
                inflight_increased = after_inflight > before_inflight
                executed = bool(pending_decreased or inflight_increased)
                approved_count = max(0, before_pending - after_pending, after_inflight - before_inflight)

                out = {
                    "executed": executed,
                    "approved_count": approved_count,
                    "result": result,
                    "skip_reason": None if executed else "entry_controller_no_order",
                    "pending_before": before_root,
                    "pending_after": after_root,
                    "pending_count_before": before_pending,
                    "pending_count_after": after_pending,
                    "inflight_before": before_inflight,
                    "inflight_after": after_inflight,
                }
                ec.logger.info("[SUMMARY AI ENTRY BRIDGE] run_entry_pipeline return normalized %s", out)
                return out

            _run_entry_pipeline_patched._summary_ai_return_bridge = True  # type: ignore[attr-defined]
            _run_entry_pipeline_patched._original_run_entry_pipeline = old_run  # type: ignore[attr-defined]
            ec.run_entry_pipeline = _run_entry_pipeline_patched
            logger.warning("[SUMMARY AI ENTRY BRIDGE] run_entry_pipeline return wrapper installed")
    except Exception:
        logger.exception("[SUMMARY AI ENTRY BRIDGE] run wrapper install failed")
        return False

    _PATCHED = True
    logger.warning("[SUMMARY AI ENTRY BRIDGE] installed")
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ENTRY BRIDGE] auto install failed")


__all__ = ["install"]
