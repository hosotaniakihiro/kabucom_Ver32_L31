# ============================================================
# File   : core/startup/summary_ai_entry_controller_bridge_patch.py
# Version: V1.6-RUN-ENTRY-PIPELINE-PART-INLINED
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI が approved/registered された後、entry_controller が None を返したり、
#   pending が減っただけで executed=True に見える問題を補正する。
#
# V1.6:
#   - run_entry_pipeline の lock-wait (SUMMARY 35秒) + 戻り値の実発注判定正規化 +
#     no-order時の1回リトライ (旧 _make_run_entry_pipeline_wrapper) は
#     trading/handlers/entry_controller.py の run_entry_pipeline 本体 (Ver2.8) へ
#     インライン化したため撤去した。ai_final_entry_check への preapproved bridge と、
#     3モジュールの _result_executed/_is_positive_order_result strict化は維持する。
#
# V1.5 (旧):
#   - pending_count 減少だけでは executed=True にしない。
#   - 実注文の根拠は order_id / sent_orders / executed_count / inflight増加等のみ。
#   - SUMMARY側の lock 待ち既定を 8秒 -> 35秒へ延長。
#     TONOSAMA controller が timeout で scheduler に戻っても thread_alive=True の間、
#     entry_controller lock を保持するケースがあるため。
#   - pending が減ったのに order根拠なしの場合は skip_reason=pending_moved_without_order とする。
# ============================================================

from __future__ import annotations

import logging
import os
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
    """実注文が確認できた場合だけ True。approved/registered/entries/pending減少だけでは成功扱いしない。"""
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            # 既存の真偽戻り値は尊重。ただし wrapper が作った dict の executed は下で order根拠付きだけを見る。
            return bool(result)
        if isinstance(result, dict):
            for key in _EXECUTED_COUNT_KEYS:
                try:
                    if int(result.get(key) or 0) > 0:
                        return True
                except Exception:
                    pass
            for key in _ORDER_KEYS:
                if _has_payload(result.get(key)):
                    return True
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


def _install_strict_result_judges() -> bool:
    ok = True
    try:
        import trading.entry.summary_ai.executor as executor
        old = getattr(executor, "_is_positive_order_result", None)
        if callable(old) and not getattr(old, "_summary_ai_strict_order_v15", False):
            def _patched_positive_order_result(result: Any) -> bool:
                return _strict_order_executed(result)
            _patched_positive_order_result._summary_ai_strict_order_v15 = True  # type: ignore[attr-defined]
            _patched_positive_order_result._original = old  # type: ignore[attr-defined]
            executor._is_positive_order_result = _patched_positive_order_result
            logger.warning("[SUMMARY AI ENTRY BRIDGE] summary_ai.executor strict real-order judge installed")
    except Exception:
        ok = False
        logger.exception("[SUMMARY AI ENTRY BRIDGE] executor strict judge install failed")

    try:
        import trading.summary.pipeline.entry_pipeline as ep
        old = getattr(ep, "_result_executed", None)
        if callable(old) and not getattr(old, "_summary_ai_strict_order_v15", False):
            def _patched_entry_pipeline_result(result: Any) -> bool:
                return _strict_order_executed(result)
            _patched_entry_pipeline_result._summary_ai_strict_order_v15 = True  # type: ignore[attr-defined]
            _patched_entry_pipeline_result._original = old  # type: ignore[attr-defined]
            ep._result_executed = _patched_entry_pipeline_result
            logger.warning("[SUMMARY AI ENTRY BRIDGE] summary.pipeline.entry_pipeline strict real-order judge installed")
    except Exception:
        ok = False
        logger.exception("[SUMMARY AI ENTRY BRIDGE] entry_pipeline strict judge install failed")

    try:
        import trading.summary.summary_entry as se
        old = getattr(se, "_result_executed", None)
        if callable(old) and not getattr(old, "_summary_ai_strict_order_v15", False):
            def _patched_summary_entry_result(result: Any) -> bool:
                return _strict_order_executed(result)
            _patched_summary_entry_result._summary_ai_strict_order_v15 = True  # type: ignore[attr-defined]
            _patched_summary_entry_result._original = old  # type: ignore[attr-defined]
            se._result_executed = _patched_summary_entry_result
            logger.warning("[SUMMARY AI ENTRY BRIDGE] summary_entry strict real-order judge installed")
    except Exception:
        ok = False
        logger.exception("[SUMMARY AI ENTRY BRIDGE] summary_entry strict judge install failed")
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

    # run_entry_pipeline の lock-wait + 戻り値正規化 (旧 _make_run_entry_pipeline_wrapper)
    # は trading/handlers/entry_controller.py の run_entry_pipeline 本体 (Ver2.8) へ
    # インライン化済みのため撤去した。

    strict_ok = _install_strict_result_judges()
    _PATCHED = True
    logger.warning("[SUMMARY AI ENTRY BRIDGE] installed v1.5 strict_real_order_ok=%s", strict_ok)
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ENTRY BRIDGE] auto install failed")


__all__ = ["install"]
