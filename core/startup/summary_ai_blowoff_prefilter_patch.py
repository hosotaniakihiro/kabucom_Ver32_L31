# ============================================================
# File   : core/startup/summary_ai_blowoff_prefilter_patch.py
# Version: V7-ENTRY-PIPELINE-COMPAT-BLOWOFF-REFILL
# ------------------------------------------------------------
# Summary-AI の Top3 選定前に危険候補を除外する。
#
# 方針:
#   - blowoff ガード自体は緩和しない。
#   - Top3前では blowoff だけを除外する。
#   - low-move は entry_pipeline / order_builder 側の厳密ガードへ一本化する。
#   - entry_pipeline と同じ detect_blowoff_top(df_summary) で判定し、
#     「前段は通過したが後段で blowoff 全落ち」を防ぐ。
#   - blowoff 除外は既定では BUY のみ。SELL は過熱後の売り候補になり得るため、
#     SUMMARY_AI_BLOWOFF_BLOCK_SELL=1 の場合だけ SELL も除外する。
# ============================================================
from __future__ import annotations

import logging
import math
import os
import threading
import time
from functools import wraps
from typing import Any, Iterable, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V7-ENTRY-PIPELINE-COMPAT-BLOWOFF-REFILL"
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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any, default: str = "BUY") -> str:
    try:
        s = str(v or default).strip().upper()
        if s in {"BUY", "LONG", "2", "買", "買い"}:
            return "BUY"
        if s in {"SELL", "SHORT", "1", "売", "売り"}:
            return "SELL"
        return default
    except Exception:
        return default


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


def _pick_side(item: Any) -> str:
    d = _as_dict(item)
    ai_row = _as_dict(d.get("ai_row"))
    src = _as_dict(d.get("source_row"))
    for root in (d, ai_row, src):
        for key in ("side", "ai_side", "entry_decision", "direction"):
            v = root.get(key)
            if v is not None and str(v).strip() != "":
                return _norm_side(v, "BUY")
    return "BUY"


def _latest_rows_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None or df.empty or "symbol" not in df.columns:
            return df
        x = df.copy()
        x["__sym_norm__"] = x["symbol"].astype(str).str.replace(r"\.0$", "", regex=True)
        if "datetime" in x.columns:
            x["__dt_sort__"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.sort_values(["__sym_norm__", "__dt_sort__"])
        else:
            x = x.reset_index().rename(columns={"index": "__dt_sort__"}).sort_values(["__sym_norm__", "__dt_sort__"])
        latest = x.groupby("__sym_norm__", as_index=False, sort=False).tail(1)
        return latest.drop(columns=[c for c in ("__sym_norm__", "__dt_sort__") if c in latest.columns], errors="ignore")
    except Exception:
        logger.debug("[SUMMARY AI PREFILTER] latest row extraction failed; using original df", exc_info=True)
        return df


def _extract_symbols_from_tops(tops: Any) -> set[str]:
    try:
        if tops is None or not isinstance(tops, pd.DataFrame) or tops.empty or "symbol" not in tops.columns:
            return set()
        return {_norm_symbol(x) for x in tops["symbol"].dropna().astype(str).tolist() if _norm_symbol(x)}
    except Exception:
        return set()


def _detect_blowoff_symbols(df_summary: Any) -> tuple[set[str], int, int]:
    """Detect blowoff symbols with the same input shape as entry_pipeline.

    V6 used latest-row-only detection.  However entry_pipeline calls
    detect_blowoff_top(df_summary) directly on the full summary frame.  When
    those two inputs disagree, Summary-AI can approve Top3, then entry_pipeline
    drops all of them as blowoff and no lower-ranked AI_OK candidate is tried.
    V7 therefore uses the full df first, matching entry_pipeline, and only
    falls back to latest rows if the full-frame detector fails open.
    """
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty:
            return set(), 0, 0
        from trading.ai.blowoff_top_detector import detect_blowoff_top

        source_rows = len(df_summary)
        latest_df = _latest_rows_per_symbol(df_summary)
        latest_rows = len(latest_df) if isinstance(latest_df, pd.DataFrame) else 0

        # Match trading.summary.pipeline.entry_pipeline._filter_blowoff.
        top_symbols = _extract_symbols_from_tops(detect_blowoff_top(df_summary))
        if top_symbols:
            return top_symbols, source_rows, latest_rows

        # Defensive fallback for unusual detector failures / empty returns.
        fallback_symbols = _extract_symbols_from_tops(detect_blowoff_top(latest_df))
        return fallback_symbols, source_rows, latest_rows
    except Exception:
        logger.exception("[SUMMARY AI PREFILTER] blowoff detect failed; fail-open")
        return set(), 0, 0


def _filter_ai_results(ai_results: Sequence[dict[str, Any]] | Iterable[Any], df_summary: Any) -> tuple[list[Any], dict[str, list[str]], set[str], dict[str, Any]]:
    items = list(ai_results or [])
    top_symbols, source_rows, latest_rows = _detect_blowoff_symbols(df_summary) if _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True) else (set(), 0, 0)
    block_sell = _env_bool("SUMMARY_AI_BLOWOFF_BLOCK_SELL", False)
    kept: list[Any] = []
    skipped: dict[str, list[str]] = {"blowoff": [], "low_move": []}
    side_counts: dict[str, int] = {}

    for item in items:
        sym = _pick_symbol(item)
        side = _pick_side(item)
        side_counts[side] = side_counts.get(side, 0) + 1
        if sym and sym in top_symbols and (side == "BUY" or block_sell):
            skipped["blowoff"].append(sym)
            continue
        kept.append(item)

    logger.warning(
        "[SUMMARY AI PREFILTER] applied before Top3 before=%s after=%s blowoff=%s low_move=%s top_symbols_count=%s source_rows=%s latest_rows=%s block_sell=%s side_counts=%s version=%s",
        len(items),
        len(kept),
        sorted(set(skipped["blowoff"])),
        sorted(set(skipped["low_move"])),
        len(top_symbols),
        source_rows,
        latest_rows,
        block_sell,
        side_counts,
        VERSION,
    )
    return kept, skipped, top_symbols, {"source_rows": source_rows, "latest_rows": latest_rows, "block_sell": block_sell, "side_counts": side_counts}


def _patch_once(reason: str = "install") -> bool:
    try:
        import trading.entry.summary_ai.executor as ex

        cur = getattr(ex, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI PREFILTER] target missing reason=%s", reason)
            return False
        if getattr(cur, "_summary_ai_blowoff_prefilter_v7", False):
            return True

        original = getattr(cur, "_original", cur)

        @wraps(original)
        def patched(ai_results, *args, **kwargs):
            df_summary = kwargs.get("df_summary")
            if df_summary is None and args:
                for a in args:
                    if isinstance(a, pd.DataFrame):
                        df_summary = a
                        break
            before_n = len(list(ai_results or []))
            filtered, skipped, top_symbols, meta = _filter_ai_results(ai_results, df_summary)
            result = original(filtered, *args, **kwargs)
            try:
                if isinstance(result, dict):
                    pre = {
                        "before": before_n,
                        "after": len(filtered),
                        "skipped": {k: sorted(set(v)) for k, v in skipped.items()},
                        "top_symbols_count": len(top_symbols),
                        "source_rows": meta.get("source_rows"),
                        "latest_rows": meta.get("latest_rows"),
                        "block_sell": meta.get("block_sell"),
                        "side_counts": meta.get("side_counts"),
                        "version": VERSION,
                    }
                    result["summary_ai_prefilter"] = pre
                    result["blowoff_prefilter"] = pre
                    if len(filtered) == 0 and before_n > 0:
                        result["skip_reason"] = "summary_ai_prefilter_all_blocked"
                        if isinstance(result.get("execution"), dict):
                            result["execution"]["skip_reason"] = "summary_ai_prefilter_all_blocked"
            except Exception:
                pass
            return result

        patched._summary_ai_blowoff_prefilter_v1 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v2 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v3 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v4 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v5 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v6 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v7 = True  # type: ignore[attr-defined]
        patched._original = original  # type: ignore[attr-defined]
        ex.execute_ai_ok_entries_bulk = patched
        logger.warning("[SUMMARY AI PREFILTER] installed reason=%s version=%s block_sell=%s", reason, VERSION, _env_bool("SUMMARY_AI_BLOWOFF_BLOCK_SELL", False))
        return True
    except Exception:
        logger.exception("[SUMMARY AI PREFILTER] install failed reason=%s", reason)
        return False


def _watcher() -> None:
    for i in range(60):
        try:
            _patch_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[SUMMARY AI PREFILTER] watcher failed", exc_info=True)
        time.sleep(1.0)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    ok = _patch_once("install")
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-prefilter-watch", daemon=True).start()
        logger.warning("[SUMMARY AI PREFILTER] watcher started")
    logger.warning("[SUMMARY AI PREFILTER] install done ok=%s version=%s", ok, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI PREFILTER] auto install failed")


__all__ = ["install", "VERSION"]
