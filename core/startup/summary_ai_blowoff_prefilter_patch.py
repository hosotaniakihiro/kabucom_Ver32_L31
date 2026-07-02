# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1"
_INSTALLED = False


def _on(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _item_symbol(x: Any) -> str:
    if not isinstance(x, dict):
        return ""
    ai = x.get("ai_row") if isinstance(x.get("ai_row"), dict) else {}
    src = x.get("source_row") if isinstance(x.get("source_row"), dict) else {}
    return _sym(x.get("symbol") or ai.get("symbol") or src.get("symbol"))


def _top_symbols(df: Any) -> set[str]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return set()
    try:
        from trading.ai.blowoff_top_detector import detect_blowoff_top
        tops = detect_blowoff_top(df)
        if tops is None or not isinstance(tops, pd.DataFrame) or tops.empty or "symbol" not in tops.columns:
            return set()
        return {_sym(x) for x in tops["symbol"].astype(str).tolist() if _sym(x)}
    except Exception:
        logger.debug("[SUMMARY AI BLOWOFF PREFILTER] detection failed", exc_info=True)
        return set()


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry.summary_ai.executor as ex
        cur = getattr(ex, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_blowoff_prefilter_v1", False):
            _INSTALLED = True
            return True

        @wraps(cur)
        def patched(ai_results, *args, **kwargs):
            if not _on("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True):
                return cur(ai_results, *args, **kwargs)
            df = kwargs.get("df_summary")
            tops = _top_symbols(df)
            if not tops:
                return cur(ai_results, *args, **kwargs)
            items = list(ai_results or [])
            kept = [x for x in items if _item_symbol(x) not in tops]
            skipped = [_item_symbol(x) for x in items if _item_symbol(x) in tops]
            if skipped:
                logger.warning("[SUMMARY AI BLOWOFF PREFILTER] before=%s after=%s skipped=%s top_count=%s version=%s", len(items), len(kept), skipped[:50], len(tops), VERSION)
            return cur(kept, *args, **kwargs)

        patched._summary_ai_blowoff_prefilter_v1 = True  # type: ignore[attr-defined]
        patched._original = cur  # type: ignore[attr-defined]
        ex.execute_ai_ok_entries_bulk = patched
        _INSTALLED = True
        logger.warning("[SUMMARY AI BLOWOFF PREFILTER] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI BLOWOFF PREFILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BLOWOFF PREFILTER] auto install failed")
