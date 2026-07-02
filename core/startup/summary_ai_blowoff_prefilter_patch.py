# ============================================================
# File   : core/startup/summary_ai_blowoff_prefilter_patch.py
# Version: V4-BLOWOFF-LOWMOVE-PREFILTER-MIN-KEEP
# ------------------------------------------------------------
# Summary-AI の Top3 選定前に危険候補を除外する。
#
# 方針:
#   - blowoff ガード自体は緩和しない。
#   - low-move ガード自体も緩和しない。
#   - executor が Top3 を選ぶ前に、df_summary / source_row / ai_row で
#     明らかに blowoff / low-move NG になる候補を候補リストから除外する。
#   - ただし全候補を0件にすると、後段の厳密ガードへ到達せず
#     approved selection rows=0 / skip=no_ai_ok で終了する。
#   - V4では全落ち時だけ、スコア上位の最小件数を後段へ渡す。
#     実発注の可否は entry_controller / liquidity / board / order_builder の
#     本来の厳密ガードに委ねる。
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

VERSION = "V4-BLOWOFF-LOWMOVE-PREFILTER-MIN-KEEP"
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


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        x = float(str(raw).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(str(raw).replace(",", "")))
    except Exception:
        return int(default)


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


def _pick_row(item: Any) -> dict[str, Any]:
    d = _as_dict(item)
    out: dict[str, Any] = {}
    for root in (_as_dict(d.get("source_row")), _as_dict(d.get("ai_row")), d):
        out.update(root)
    return out


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        try:
            v = row.get(key)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _item_score(item: Any) -> float:
    row = _pick_row(item)
    vals = []
    for key in (
        "score_total",
        "total_score",
        "final_score",
        "display_score",
        "score",
        "score_buy",
        "buy_score",
        "score_sell",
        "sell_score",
        "confidence",
        "ai_confidence",
    ):
        vals.append(_safe_float(row.get(key), 0.0))
    try:
        raw = _as_dict(row.get("_raw"))
        for key in ("score_total_raw", "score_buy_raw", "buy_score_raw", "pending_score"):
            vals.append(_safe_float(raw.get(key), 0.0))
    except Exception:
        pass
    return max(vals) if vals else 0.0


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
        logger.exception("[SUMMARY AI PREFILTER] blowoff detect failed; fail-open")
        return set()


def _low_move_ng(item: Any, df_summary: Any) -> tuple[bool, str, dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_LOWMOVE_PREFILTER_ENABLED", True):
        return False, "disabled", {}
    sym = _pick_symbol(item)
    row = _pick_row(item)

    try:
        if sym and isinstance(df_summary, pd.DataFrame) and not df_summary.empty and "symbol" in df_summary.columns:
            x = df_summary[df_summary["symbol"].astype(str).str.replace(r"\.0$", "", regex=True) == sym]
            if not x.empty:
                try:
                    if "datetime" in x.columns:
                        x = x.sort_values("datetime")
                    latest = x.iloc[-1].to_dict()
                    tmp = dict(row)
                    tmp.update(latest)
                    row = tmp
                except Exception:
                    pass
    except Exception:
        pass

    close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
    atr = _safe_float(_first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0), 0.0)
    min_range_pct = _env_float("ENTRY_ORDER_MIN_RANGE_PCT", 0.005)
    min_atr_ratio = _env_float("ENTRY_ORDER_MIN_ATR_RATIO", 0.0035)

    if close <= 0:
        return False, "no_close_fail_open", {"symbol": sym, "close": close}

    range_pct = None
    if high > 0 and low > 0 and high >= low:
        range_pct = (high - low) / close
    else:
        rp = _safe_float(_first(row, ("range_pct", "day_range_pct", "intraday_range_pct"), 0.0), 0.0)
        if rp > 1.0:
            rp = rp / 100.0
        if rp > 0:
            range_pct = rp

    if range_pct is not None and range_pct < min_range_pct:
        return True, "low_move_range", {
            "symbol": sym,
            "close": close,
            "high": high,
            "low": low,
            "range_pct": range_pct,
            "min_range_pct": min_range_pct,
        }

    if atr > 0:
        atr_ratio = atr / close
        if atr_ratio < min_atr_ratio:
            return True, "low_move_atr", {
                "symbol": sym,
                "close": close,
                "atr": atr,
                "atr_ratio": atr_ratio,
                "min_atr_ratio": min_atr_ratio,
            }

    return False, "ok", {"symbol": sym, "range_pct": range_pct, "atr": atr}


def _min_keep_items(items: list[Any], skipped: dict[str, list[str]]) -> list[Any]:
    if not _env_bool("SUMMARY_AI_PREFILTER_MIN_KEEP_ENABLED", True):
        return []
    min_keep = max(0, _env_int("SUMMARY_AI_PREFILTER_MIN_KEEP", 3))
    if min_keep <= 0 or not items:
        return []
    allow_blowoff = _env_bool("SUMMARY_AI_PREFILTER_MIN_KEEP_ALLOW_BLOWOFF", False)
    blowoff_set = set(skipped.get("blowoff") or [])
    candidates = []
    for item in items:
        sym = _pick_symbol(item)
        if not allow_blowoff and sym in blowoff_set:
            continue
        candidates.append(item)
    if not candidates and allow_blowoff:
        candidates = list(items)
    if not candidates:
        return []
    return sorted(candidates, key=_item_score, reverse=True)[:min_keep]


def _filter_ai_results(ai_results: Sequence[dict[str, Any]] | Iterable[Any], df_summary: Any) -> tuple[list[Any], dict[str, list[str]], set[str]]:
    items = list(ai_results or [])
    top_symbols = _detect_blowoff_symbols(df_summary) if _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True) else set()
    kept: list[Any] = []
    skipped: dict[str, list[str]] = {"blowoff": [], "low_move": [], "min_keep": []}
    low_details: list[dict[str, Any]] = []

    for item in items:
        sym = _pick_symbol(item)
        if sym and sym in top_symbols:
            skipped["blowoff"].append(sym)
            continue
        ng, why, detail = _low_move_ng(item, df_summary)
        if ng:
            skipped["low_move"].append(sym or "UNKNOWN")
            d = dict(detail or {})
            d["reason"] = why
            low_details.append(d)
            continue
        kept.append(item)

    if not kept and items:
        restored = _min_keep_items(items, skipped)
        if restored:
            kept = restored
            skipped["min_keep"] = [_pick_symbol(x) or "UNKNOWN" for x in restored]
            logger.warning(
                "[SUMMARY AI PREFILTER] all candidates filtered -> min_keep restored symbols=%s before=%s low_move=%s blowoff=%s version=%s",
                skipped["min_keep"],
                len(items),
                sorted(set(skipped.get("low_move") or [])),
                sorted(set(skipped.get("blowoff") or [])),
                VERSION,
            )

    if skipped["blowoff"] or skipped["low_move"] or skipped["min_keep"]:
        logger.warning(
            "[SUMMARY AI PREFILTER] applied before Top3 before=%s after=%s blowoff=%s low_move=%s min_keep=%s low_details=%s top_symbols_count=%s version=%s",
            len(items),
            len(kept),
            sorted(set(skipped["blowoff"])),
            sorted(set(skipped["low_move"])),
            sorted(set(skipped["min_keep"])),
            low_details[:20],
            len(top_symbols),
            VERSION,
        )
    return kept, skipped, top_symbols


def _patch_once(reason: str = "install") -> bool:
    try:
        import trading.entry.summary_ai.executor as ex

        cur = getattr(ex, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI PREFILTER] target missing reason=%s", reason)
            return False
        if getattr(cur, "_summary_ai_blowoff_prefilter_v4", False):
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
            filtered, skipped, top_symbols = _filter_ai_results(ai_results, df_summary)
            result = original(filtered, *args, **kwargs)
            try:
                if isinstance(result, dict):
                    result["summary_ai_prefilter"] = {
                        "before": len(list(ai_results or [])),
                        "after": len(filtered),
                        "skipped": {k: sorted(set(v)) for k, v in skipped.items()},
                        "top_symbols_count": len(top_symbols),
                        "version": VERSION,
                    }
                    result["blowoff_prefilter"] = result["summary_ai_prefilter"]
            except Exception:
                pass
            return result

        patched._summary_ai_blowoff_prefilter_v1 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v2 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v3 = True  # type: ignore[attr-defined]
        patched._summary_ai_blowoff_prefilter_v4 = True  # type: ignore[attr-defined]
        patched._original = original  # type: ignore[attr-defined]
        ex.execute_ai_ok_entries_bulk = patched
        logger.warning("[SUMMARY AI PREFILTER] installed reason=%s version=%s", reason, VERSION)
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
    os.environ.setdefault("SUMMARY_AI_PREFILTER_MIN_KEEP_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_PREFILTER_MIN_KEEP", "3")
    os.environ.setdefault("SUMMARY_AI_PREFILTER_MIN_KEEP_ALLOW_BLOWOFF", "0")
    ok = _patch_once("install")
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-prefilter-watch", daemon=True).start()
        logger.warning("[SUMMARY AI PREFILTER] watcher started")
    logger.warning("[SUMMARY AI PREFILTER] install done ok=%s version=%s min_keep=%s", ok, VERSION, os.getenv("SUMMARY_AI_PREFILTER_MIN_KEEP"))
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI PREFILTER] auto install failed")


__all__ = ["install", "VERSION"]
