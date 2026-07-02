# ============================================================
# File   : core/startup/summary_ai_blowoff_prefilter_patch.py
# Version: V5-BLOWOFF-ONLY-BEFORE-TOP3-LOWMOVE-LATE
# ------------------------------------------------------------
# Summary-AI の Top3 選定前に危険候補を除外する。
#
# 方針:
#   - blowoff ガード自体は緩和しない。
#   - Top3前では blowoff だけを除外する。
#   - low-move は entry_pipeline 側の「3件cap直前」のprefilterへ一本化する。
#     ここで low-move まで除外すると、AI_OK候補が全消えして
#     approved=0 / no_ai_ok になり、次候補補充が効かなくなるため。
#   - 互換用に SUMMARY_AI_LOWMOVE_PREFILTER_EARLY_ENABLED=1 の時だけ
#     旧 early low-move を有効化できるが、既定は 0。
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

VERSION = "V5-BLOWOFF-ONLY-BEFORE-TOP3-LOWMOVE-LATE"
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
    # 既定では early low-move は無効。entry_pipeline側で3件cap直前に判定する。
    if not _env_bool("SUMMARY_AI_LOWMOVE_PREFILTER_EARLY_ENABLED", False):
        return False, "early_lowmove_disabled", {}
    sym = _pick_symbol(item)
    row = _pick_row(item)

    try:
        if sym and isinstance(df_summary, pd.DataFrame) and not df_summary.empty and "symbol" in df_summary.columns:
            x = df_summary[df_summary["symbol"].astype(str).str.replace(r"\.0$", "", regex=True) == sym]
            if not x.empty:
                if "datetime" in x.columns:
                    x = x.sort_values("datetime")
                tmp = dict(row)
                tmp.update(x.iloc[-1].to_dict())
                row = tmp
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


def _filter_ai_results(ai_results: Sequence[dict[str, Any]] | Iterable[Any], df_summary: Any) -> tuple[list[Any], dict[str, list[str]], set[str]]:
    items = list(ai_results or [])
    top_symbols = _detect_blowoff_symbols(df_summary) if _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True) else set()
    kept: list[Any] = []
    skipped: dict[str, list[str]] = {"blowoff": [], "low_move": []}
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

    if skipped["blowoff"] or skipped["low_move"]:
        logger.warning(
            "[SUMMARY AI PREFILTER] applied before Top3 before=%s after=%s blowoff=%s low_move=%s low_details=%s top_symbols_count=%s early_lowmove=%s version=%s",
            len(items),
            len(kept),
            sorted(set(skipped["blowoff"])),
            sorted(set(skipped["low_move"])),
            low_details[:20],
            len(top_symbols),
            _env_bool("SUMMARY_AI_LOWMOVE_PREFILTER_EARLY_ENABLED", False),
            VERSION,
        )
    else:
        logger.info(
            "[SUMMARY AI PREFILTER] no early skip before=%s top_symbols_count=%s early_lowmove=%s version=%s",
            len(items),
            len(top_symbols),
            _env_bool("SUMMARY_AI_LOWMOVE_PREFILTER_EARLY_ENABLED", False),
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
        if getattr(cur, "_summary_ai_blowoff_prefilter_v5", False):
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
            filtered, skipped, top_symbols = _filter_ai_results(ai_results, df_summary)
            result = original(filtered, *args, **kwargs)
            try:
                if isinstance(result, dict):
                    pre = {
                        "before": before_n,
                        "after": len(filtered),
                        "skipped": {k: sorted(set(v)) for k, v in skipped.items()},
                        "top_symbols_count": len(top_symbols),
                        "early_lowmove": _env_bool("SUMMARY_AI_LOWMOVE_PREFILTER_EARLY_ENABLED", False),
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
        patched._original = original  # type: ignore[attr-defined]
        ex.execute_ai_ok_entries_bulk = patched
        logger.warning("[SUMMARY AI PREFILTER] installed reason=%s version=%s early_lowmove=%s", reason, VERSION, _env_bool("SUMMARY_AI_LOWMOVE_PREFILTER_EARLY_ENABLED", False))
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
