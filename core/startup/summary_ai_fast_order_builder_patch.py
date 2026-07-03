# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_fast_order_builder_patch.py
# Version: V1-SUMMARY-AI-FAST-ORDER-BUILDER
# ------------------------------------------------------------
# SUMMARY_AI が AI_OK → qty算出まで進んだあと、発注直前の
# board retry で entry_controller 側の snapshot 判定より遅くなり、
# snapshot_no_order になる問題を抑止する。
#
# 方針:
#   - 判定閾値は緩和しない。
#   - 板が無い時は既存 entry_order_builder の close 指値 fallback を使う。
#   - board retry の初期待ち時間だけ短縮して、ORDER_BUILD_OK / ENTRY_DISPATCH
#     まで 1 サイクル内に進める。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-FAST-ORDER-BUILDER"
_INSTALLED = False
_ORIGINAL_BUILD_ENTRY_ORDER = None


def _safe_float(v: Any, default: float) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _set_cap(obj: Any, name: str, cap: float) -> tuple[float | None, float]:
    old = None
    try:
        old = float(getattr(obj, name))
    except Exception:
        pass
    new = min(old if old is not None else cap, cap)
    try:
        setattr(obj, name, new)
    except Exception:
        pass
    return old, new


def install() -> bool:
    global _INSTALLED, _ORIGINAL_BUILD_ENTRY_ORDER
    if _INSTALLED:
        return True
    try:
        # import 前のデフォルトも短縮する。既にユーザーがさらに短い値を入れている場合は尊重する。
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_SEC", "0.8")
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.2")

        from trading.handlers import entry_order_builder as eob

        old_retry, new_retry = _set_cap(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", _safe_float(os.environ.get("ENTRY_ORDER_BOARD_RETRY_SEC"), 0.8))
        old_interval, new_interval = _set_cap(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", _safe_float(os.environ.get("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"), 0.2))

        cur = getattr(eob, "build_entry_order", None)
        if callable(cur) and not getattr(cur, "_summary_ai_fast_order_builder_v1", False):
            _ORIGINAL_BUILD_ENTRY_ORDER = getattr(cur, "_original", cur)

            def _patched_build_entry_order(*args, **kwargs):
                source = str(kwargs.get("source") or "").upper()
                symbol = kwargs.get("symbol")
                side = kwargs.get("side")
                if source == "SUMMARY_AI":
                    logger.info(
                        "[SUMMARY AI FAST ORDER BUILDER] start symbol=%s side=%s retry_sec=%s retry_interval=%s version=%s",
                        symbol,
                        side,
                        getattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", None),
                        getattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", None),
                        VERSION,
                    )
                result = _ORIGINAL_BUILD_ENTRY_ORDER(*args, **kwargs)
                if source == "SUMMARY_AI":
                    logger.info(
                        "[SUMMARY AI FAST ORDER BUILDER] done symbol=%s side=%s ok=%s reason=%s detail=%s version=%s",
                        symbol,
                        side,
                        isinstance(result, dict) and result.get("ok"),
                        result.get("reason") if isinstance(result, dict) else type(result).__name__,
                        result.get("detail") if isinstance(result, dict) else None,
                        VERSION,
                    )
                return result

            _patched_build_entry_order._summary_ai_fast_order_builder_v1 = True  # type: ignore[attr-defined]
            _patched_build_entry_order._original = _ORIGINAL_BUILD_ENTRY_ORDER  # type: ignore[attr-defined]
            eob.build_entry_order = _patched_build_entry_order

            try:
                import trading.handlers.entry_controller as ec
                ec.build_entry_order = _patched_build_entry_order
            except Exception:
                logger.debug("[SUMMARY AI FAST ORDER BUILDER] entry_controller alias patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI FAST ORDER BUILDER] installed version=%s retry_sec %s->%s interval %s->%s",
            VERSION,
            old_retry,
            new_retry,
            old_interval,
            new_interval,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI FAST ORDER BUILDER] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI FAST ORDER BUILDER] auto install failed")


__all__ = ["install", "VERSION"]
