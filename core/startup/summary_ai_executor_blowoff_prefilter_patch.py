# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_executor_blowoff_prefilter_patch.py
# Version: V1-SUMMARY-AI-EXECUTOR-BLOWOFF-PREFILTER
# ------------------------------------------------------------
# Purpose:
#   Prevent Summary-AI approved slots from being wasted by candidates that the
#   downstream entry_pipeline will immediately reject as blowoff.
#
# This does not relax any guard.  It applies the existing blowoff detector before
# approved selection so rolling retry can move to the next AI_OK candidate.
# ============================================================
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-EXECUTOR-BLOWOFF-PREFILTER"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _as_dict(v: Any) -> dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _symbol_from_item(ex: Any, item: dict[str, Any]) -> str:
    try:
        return str(ex._pick_symbol(item) or "").strip()
    except Exception:
        ai = _as_dict(item.get("ai_row"))
        src = _as_dict(item.get("source_row"))
        return str(item.get("symbol") or ai.get("symbol") or src.get("symbol") or "").strip()


def _row_for_detector(item: dict[str, Any]) -> dict[str, Any]:
    ai = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    row = {}
    row.update(src)
    row.update(ai)
    row.update(item)
    # detector expects close/volume/rsi/vwap-ish columns; keep conservative fallbacks
    if "close" not in row:
        row["close"] = row.get("close_price") or row.get("price") or row.get("current_price")
    if "volume" not in row:
        row["volume"] = row.get("trading_volume") or row.get("Volume") or 0
    if "rsi" not in row:
        row["rsi"] = row.get("RSI") or 50
    if "vwap" not in row:
        row["vwap"] = row.get("day_vwap") or row.get("close") or row.get("price") or row.get("close_price") or 0
    return row


def _detect_blowoff_symbols(items: list[dict[str, Any]]) -> set[str]:
    try:
        import pandas as pd
        from trading.ai.blowoff_top_detector import detect_blowoff_top
        rows = [_row_for_detector(x) for x in items if isinstance(x, dict)]
        if not rows:
            return set()
        df = pd.DataFrame(rows)
        if df.empty or "symbol" not in df.columns:
            return set()
        out = detect_blowoff_top(df)
        if out is None or getattr(out, "empty", True):
            return set()
        return {str(x).strip() for x in out.get("symbol", []).tolist() if str(x).strip()}
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR BLOWOFF PREFILTER] detector failed", exc_info=True)
        return set()


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_EXECUTOR_BLOWOFF_PREFILTER", True):
        logger.warning("[SUMMARY AI EXECUTOR BLOWOFF PREFILTER] disabled by env")
        return False
    try:
        import trading.entry.summary_ai.executor as ex
        cur = getattr(ex, "_base_filter_blocked_ai_ok_items", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI EXECUTOR BLOWOFF PREFILTER] target missing")
            return False
        if getattr(cur, "_summary_ai_executor_blowoff_prefilter_v1", False):
            _INSTALLED = True
            return True

        @wraps(cur)
        def _base_filter_with_blowoff(ok_items: list[dict[str, Any]]):
            kept = cur(ok_items)
            try:
                if not kept:
                    return kept
                blowoff_symbols = _detect_blowoff_symbols(list(kept))
                if not blowoff_symbols:
                    return kept
                out = []
                skipped = []
                for item in kept:
                    sym = _symbol_from_item(ex, item)
                    if sym in blowoff_symbols:
                        skipped.append({
                            "symbol": sym,
                            "side": getattr(ex, "_pick_side", lambda x: x.get("side", "BUY"))(item),
                            "reason": "blowoff_prefilter",
                            "score": getattr(ex, "_score_for_side", lambda x: 0.0)(item),
                        })
                        continue
                    out.append(item)
                if skipped:
                    logger.warning(
                        "[SUMMARY AI EXECUTOR BLOWOFF PREFILTER] before=%s after=%s skipped=%s version=%s",
                        len(kept), len(out), skipped[:50], VERSION,
                    )
                return out
            except Exception:
                logger.exception("[SUMMARY AI EXECUTOR BLOWOFF PREFILTER] filter failed; use core kept")
                return kept

        _base_filter_with_blowoff._summary_ai_executor_blowoff_prefilter_v1 = True  # type: ignore[attr-defined]
        _base_filter_with_blowoff._original = cur  # type: ignore[attr-defined]
        ex._base_filter_blocked_ai_ok_items = _base_filter_with_blowoff
        # Keep compatibility hook aligned with patched base filter.
        def _filter_blocked_ai_ok_items(ok_items):
            return ex._base_filter_blocked_ai_ok_items(ok_items)
        _filter_blocked_ai_ok_items._summary_ai_executor_core_filter_rev9 = True  # type: ignore[attr-defined]
        _filter_blocked_ai_ok_items._summary_ai_executor_blowoff_prefilter_v1 = True  # type: ignore[attr-defined]
        ex._filter_blocked_ai_ok_items = _filter_blocked_ai_ok_items
        try:
            ex._CORE_FILTER_FUNC = _filter_blocked_ai_ok_items
        except Exception:
            pass
        _INSTALLED = True
        logger.warning("[SUMMARY AI EXECUTOR BLOWOFF PREFILTER] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR BLOWOFF PREFILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI EXECUTOR BLOWOFF PREFILTER] auto install failed")

__all__ = ["VERSION", "install"]
