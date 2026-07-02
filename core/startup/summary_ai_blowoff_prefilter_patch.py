# ============================================================
# File   : core/startup/summary_ai_blowoff_prefilter_patch.py
# Version: V2-BLOWOFF-BEFORE-TOP3-SELECTION
# ------------------------------------------------------------
# Summary-AI の Top3 選定前に blowoff top 候補を除外する。
#
# 目的:
#   従来は executor が AI_OK Top3 を先に選び、その後 entry_pipeline 側で
#   blowoff top を除外していた。そのため Top3 が全て blowoff の場合、
#   AI_OK 候補が他に残っていても executable=0 になっていた。
#
# 方針:
#   - blowoff ガード自体は緩和しない。
#   - execute_ai_ok_entries_bulk に入る ai_results を、Top3選定前に
#     df_summary の blowoff top から除外する。
#   - 除外後の残り候補から既存 executor が Top3 を選ぶ。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from functools import wraps
from typing import Any, Iterable, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V2-BLOWOFF-BEFORE-TOP3-SELECTION"
_INSTALLED = False
_WATCHER_STARTED = False
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_VALUES:
            return True
        if s in _FALSE_VALUES:
            return False
    except Exception:
        pass
    return bool(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _as_dict(v: Any) -> dict[str, Any]:
    try:
        if isinstance(v, dict):
            return v
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _pick_symbol(item: Any) -> str:
    d = _as_dict(item)
    ai_row = _as_dict(d.get("ai_row"))
    src = _as_dict(d.get("source_row"))
    for root in (d, ai_row, src):
        for key in ("symbol", "Symbol", "code", "stock_code", "銘柄コード"):
            sym = _norm_symbol(root.get(key))
            if sym:
                return sym
    return ""


def _detect_blowoff_symbols(df_summary: Any) -> set[str]:
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty:
            return set()
        from trading.ai.blowoff_top_detector import detect_blowoff_top

        tops = detect_blowoff_top(df_summary)
        if tops is None or not isinstance(tops, pd.DataFrame) or tops.empty or "symbol" not in tops.columns:
            return set()
        return {_norm_symbol(x) for x in tops["symbol"].dropna().astype(str).tolist() if _norm_symbol(x)}
    except Exception:
        logger.exception("[SUMMARY AI BLOWOFF PREFILTER] detect failed; fail-open")
        return set()


def _filter_ai_results(ai_results: Sequence[dict[str, Any]] | Iterable[Any], df_summary: Any) -> tuple[list[Any], list[str], set[str]]:
    items = list(ai_results or [])
    if not _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True):
        return items, [], set()
    top_symbols = _detect_blowoff_symbols(df_summary)
    if not top_symbols:
        return items, [], top_symbols
    kept: list[Any] = []
    skipped: list[str] = []
    for item in items:
        sym = _pick_symbol(item)
        if sym and sym in top_symbols:
            skipped.append(sym)
            continue
        kept.append(item)
    if skipped:
        logger.warning(
            "[SUMMARY AI BLOWOFF PREFILTER] applied before Top3 before=%s after=%s skipped=%s top_symbols_count=%s version=%s",
            len(items),
            len(kept),
            sorted(set(skipped)),
            len(top_symbols),
            VERSION,
        )
    return kept, skipped, top_symbols


def _patch_once(reason: str = "install") -> bool:
    try:
        import trading.entry.summary_ai.executor as ex

        cur = getattr(ex, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI BLOWOFF PREFILTER] target missing reason=%s", reason)
            return False
        if getattr(cur, "_summary_ai_blowoff_prefilter_v2", False):
            return True

        original = getattr(cur, "_original", cur)

        @wraps(original)
        def patched(ai_results, *args, **kwargs):
            df_summary = kwargs.get("df_summary")
            if df_summary is None and args:
                # execute_ai_ok_entries_bulk は通常 keyword-only df_summary だが、
                # runtime patch 経由の互換呼び出しにも耐える。
                for a in args:
                    if isinstance(a, pd.DataFrame):
                        df_summary = a
                        break
            filtered, skipped, top_symbols = _filter_ai_results(ai_results, df_summary)
            result = original(filtered, *args, **kwargs)
            try:
                if isinstance(result, dict):
                    result["blowoff_prefilter"] = {
                        "enabled": _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True),
                        "before": len(list(ai_results or [])),
                        "after": len(filtered),
                        "skipped": sorted(set(skipped)),
                        "top_symbols_count": len(top_symbols),
                        "version": VERSION,
                    }
            except Exception:
                pass
            return result

        patched._summary_ai_blowoff_prefilter_v1 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v2 = True  # type: ignore[attr-defined]
        patched._original = original  # type: ignore[attr-defined]
        ex.execute_ai_ok_entries_bulk = patched
        logger.warning("[SUMMARY AI BLOWOFF PREFILTER] installed reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI BLOWOFF PREFILTER] install failed reason=%s", reason)
        return False


def _watcher() -> None:
    for i in range(60):
        try:
            _patch_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[SUMMARY AI BLOWOFF PREFILTER] watcher failed", exc_info=True)
        time.sleep(1.0)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True):
        logger.warning("[SUMMARY AI BLOWOFF PREFILTER] disabled by env")
        return False
    ok = _patch_once("install")
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-blowoff-prefilter-watch", daemon=True).start()
        logger.warning("[SUMMARY AI BLOWOFF PREFILTER] watcher started")
    logger.warning("[SUMMARY AI BLOWOFF PREFILTER] install done ok=%s version=%s", ok, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BLOWOFF PREFILTER] auto install failed")


__all__ = ["install", "VERSION"]
