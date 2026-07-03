# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_board_missing_limit_fallback_patch.py
# Version: V1-SUMMARY-AI-STRICT-BOARD-MISSING-LIMIT-FALLBACK
# ------------------------------------------------------------
# SUMMARY_AI が AI_OK/approved/MTF/low-move を通過した後、
# 板取得だけ失敗して STRICT_BOARD_MISSING で snapshot_no_order になる場合、
# 成行ではなく close/current_price/vwap から保守的な LIMIT を作る既存fallbackを
# SUMMARY_AI に限って再試行する。
#
# 目的:
#   - 板API/PUSHローテの一時欠落だけで発注機会を失わない
#   - 成行発注は使わない
#   - low_move/MTF/5秒/流動性ガードは既存処理を維持
# ============================================================
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-STRICT-BOARD-MISSING-LIMIT-FALLBACK"
_INSTALLED = False

_SUMMARY_SOURCES = {"SUMMARY_AI", "SUMMARY", "PUSH", "PUSH_SUMMARY", "STOCK_SUMMARY"}


def _is_summary_ai_call(kwargs: dict[str, Any]) -> bool:
    try:
        source = str(kwargs.get("source") or "").strip().upper()
        row = kwargs.get("entry_row") if isinstance(kwargs.get("entry_row"), dict) else {}
        row_source = str(row.get("source") or "").strip().upper()
        entry_type = str(row.get("entry_type") or row.get("pipeline_source") or kwargs.get("entry_type") or "").strip().upper()
        text = "|".join(str(x or "").upper() for x in (source, row_source, entry_type, row.get("reason"), row.get("ai_reason")))
        return source in _SUMMARY_SOURCES or row_source in _SUMMARY_SOURCES or "SUMMARY_AI" in text or "SRC=SUMMARY" in text
    except Exception:
        return False


def _has_price_source(kwargs: dict[str, Any]) -> bool:
    try:
        row = kwargs.get("entry_row") if isinstance(kwargs.get("entry_row"), dict) else {}
        for k in ("close_price", "price", "current_price", "close", "vwap"):
            v = row.get(k)
            if v is None or str(v).strip() == "":
                continue
            if float(str(v).replace(",", "")) > 0:
                return True
    except Exception:
        pass
    return False


def _patch_aliases(eob: Any, patched: Any) -> None:
    try:
        eob.build_entry_order = patched
    except Exception:
        pass
    try:
        import trading.handlers.entry_controller as ec
        ec.build_entry_order = patched
    except Exception:
        pass


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from trading.handlers import entry_order_builder as eob
        cur = getattr(eob, "build_entry_order", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI BOARD FALLBACK] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_board_missing_limit_fallback_v1", False):
            _INSTALLED = True
            return True
        base = cur

        def _patched_build_entry_order(*args, **kwargs):
            result = base(*args, **kwargs)
            try:
                reason = result.get("reason") if isinstance(result, dict) else None
                if reason != "STRICT_BOARD_MISSING" or not _is_summary_ai_call(kwargs):
                    return result
                if not _has_price_source(kwargs):
                    logger.warning(
                        "[SUMMARY AI BOARD FALLBACK] strict board missing but no price source symbol=%s side=%s version=%s",
                        kwargs.get("symbol"), kwargs.get("side"), VERSION,
                    )
                    return result

                old_require = getattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", None)
                try:
                    setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", False)
                    retry = base(*args, **kwargs)
                finally:
                    try:
                        if old_require is not None:
                            setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", old_require)
                    except Exception:
                        pass

                if isinstance(retry, dict) and retry.get("ok"):
                    detail = retry.setdefault("detail", {})
                    if isinstance(detail, dict):
                        detail["board_missing_limit_fallback"] = True
                        detail["original_reason"] = reason
                    logger.warning(
                        "[SUMMARY AI BOARD FALLBACK] converted STRICT_BOARD_MISSING to LIMIT fallback symbol=%s side=%s detail=%s version=%s",
                        kwargs.get("symbol"), kwargs.get("side"), detail, VERSION,
                    )
                    return retry
                logger.warning(
                    "[SUMMARY AI BOARD FALLBACK] fallback retry still NG symbol=%s side=%s retry_reason=%s version=%s",
                    kwargs.get("symbol"), kwargs.get("side"), retry.get("reason") if isinstance(retry, dict) else type(retry).__name__, VERSION,
                )
                return result
            except Exception:
                logger.exception("[SUMMARY AI BOARD FALLBACK] retry failed symbol=%s side=%s version=%s", kwargs.get("symbol"), kwargs.get("side"), VERSION)
                return result

        _patched_build_entry_order._summary_ai_board_missing_limit_fallback_v1 = True  # type: ignore[attr-defined]
        _patched_build_entry_order._original = base  # type: ignore[attr-defined]
        _patch_aliases(eob, _patched_build_entry_order)
        _INSTALLED = True
        logger.warning("[SUMMARY AI BOARD FALLBACK] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI BOARD FALLBACK] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BOARD FALLBACK] auto install failed")


__all__ = ["install", "VERSION"]
