# ============================================================
# File   : core/startup/summary_ai_blowoff_prefilter_patch.py
# Version: V9-REENTRANT-SAFE-BLOWOFF-PREFILTER
# ------------------------------------------------------------
# Summary-AI の Top3 選定前と entry_pipeline 発注直前の blowoff 過剰除外を抑制する。
#
# 方針:
#   - blowoff ガード自体は削除しない。
#   - detect_blowoff_top() の symbol 一致だけでは除外しない。
#   - RSI過熱 + 大きい値幅 + 高値圏/終値位置 + 方向の複合条件で、
#     本当に危険な天井候補だけ除外する。
#   - Summary-AI の SELL は既定では blowoff 除外しない。
#   - async/direct_dispatch など他パッチとのラップ順が変わっても、
#     同じ prefilter を二重・多重に掛けず、再帰ループを作らない。
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

VERSION = "V9-REENTRANT-SAFE-BLOWOFF-PREFILTER"
_INSTALLED = False
_WATCHER_STARTED = False
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}
_BLOWOFF_MARKERS = tuple(f"_summary_ai_blowoff_prefilter_v{i}" for i in range(1, 10))
_ENTRY_PIPELINE_MARKERS = tuple(f"_summary_ai_blowoff_entry_pipeline_v{i}" for i in range(1, 10))


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


def _latest_row_map(df_summary: Any) -> dict[str, dict[str, Any]]:
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty or "symbol" not in df_summary.columns:
            return {}
        latest = _latest_rows_per_symbol(df_summary)
        if latest is None or not isinstance(latest, pd.DataFrame) or latest.empty:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for _, row in latest.iterrows():
            d = _as_dict(row)
            sym = _norm_symbol(d.get("symbol"))
            if sym:
                out[sym] = d
        return out
    except Exception:
        logger.debug("[SUMMARY AI PREFILTER] latest row map failed", exc_info=True)
        return {}


def _extract_symbols_from_tops(tops: Any) -> set[str]:
    try:
        if tops is None or not isinstance(tops, pd.DataFrame) or tops.empty or "symbol" not in tops.columns:
            return set()
        return {_norm_symbol(x) for x in tops["symbol"].dropna().astype(str).tolist() if _norm_symbol(x)}
    except Exception:
        return set()


def _range_pct_value(row: dict[str, Any], *, close: float, high: float, low: float) -> float:
    raw = 0.0
    for key in ("range_pct", "intrabar_range_pct", "_intrabar_range_pct"):
        raw = _safe_float(row.get(key), 0.0)
        if raw > 0:
            break
    if raw > 0:
        return raw * 100.0 if raw <= 0.5 else raw
    base = close if close > 0 else max(high, low, 1.0)
    if high > 0 and low > 0 and high > low and base > 0:
        return abs(high - low) / base * 100.0
    return 0.0


def _close_position(row: dict[str, Any], *, close: float, high: float, low: float) -> float:
    if high > low and close > 0:
        return max(0.0, min(1.0, (close - low) / max(high - low, 1e-9)))
    return 0.5


def _is_dangerous_blowoff_row(row: dict[str, Any], *, side: str) -> tuple[bool, dict[str, Any]]:
    close = _safe_float(row.get("close") or row.get("close_price") or row.get("price") or row.get("current_price"), 0.0)
    high = _safe_float(row.get("high") or row.get("high_price") or row.get("day_high"), 0.0)
    low = _safe_float(row.get("low") or row.get("low_price") or row.get("day_low"), 0.0)
    rsi = _safe_float(row.get("rsi"), 50.0)
    slope = _safe_float(row.get("slope_atr_scaled") or row.get("slope") or row.get("score_slope"), 0.0)
    range_pct = _range_pct_value(row, close=close, high=high, low=low)
    close_pos = _close_position(row, close=close, high=high, low=low)
    min_rsi = _env_float("SUMMARY_AI_BLOWOFF_DANGER_RSI", 80.0)
    min_range = _env_float("SUMMARY_AI_BLOWOFF_DANGER_RANGE_PCT", 2.5)
    min_close_pos = _env_float("SUMMARY_AI_BLOWOFF_DANGER_CLOSE_POS", 0.85)

    side = _norm_side(side, "BUY")
    if side == "BUY":
        ok = rsi >= min_rsi and range_pct >= min_range and slope > 0 and close_pos >= min_close_pos
    else:
        ok = rsi <= (100.0 - min_rsi) and range_pct >= min_range and slope < 0 and close_pos <= (1.0 - min_close_pos)
    return bool(ok), {
        "side": side,
        "rsi": round(rsi, 3),
        "range_pct": round(range_pct, 3),
        "slope": round(slope, 6),
        "close_pos": round(close_pos, 3),
        "close": close,
        "high": high,
        "low": low,
        "need_rsi": min_rsi,
        "need_range_pct": min_range,
        "need_close_pos": min_close_pos,
    }


def _detect_blowoff_symbols(df_summary: Any) -> tuple[set[str], int, int, dict[str, dict[str, Any]]]:
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty:
            return set(), 0, 0, {}
        from trading.ai.blowoff_top_detector import detect_blowoff_top

        source_rows = len(df_summary)
        latest_df = _latest_rows_per_symbol(df_summary)
        latest_rows = len(latest_df) if isinstance(latest_df, pd.DataFrame) else 0
        latest_map = _latest_row_map(latest_df)

        top_symbols = _extract_symbols_from_tops(detect_blowoff_top(df_summary))
        if not top_symbols:
            top_symbols = _extract_symbols_from_tops(detect_blowoff_top(latest_df))
        return top_symbols, source_rows, latest_rows, latest_map
    except Exception as e:
        # 再帰エラー時に logger.exception の traceback 整形でさらに再帰するのを避ける。
        logger.error("[SUMMARY AI PREFILTER] blowoff detect failed; fail-open err=%s", e)
        return set(), 0, 0, {}


def _filter_ai_results(ai_results: Sequence[dict[str, Any]] | Iterable[Any], df_summary: Any) -> tuple[list[Any], dict[str, list[str]], set[str], dict[str, Any]]:
    items = list(ai_results or [])
    top_symbols, source_rows, latest_rows, latest_map = _detect_blowoff_symbols(df_summary) if _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True) else (set(), 0, 0, {})
    block_sell = _env_bool("SUMMARY_AI_BLOWOFF_BLOCK_SELL", False)
    kept: list[Any] = []
    skipped: dict[str, list[str]] = {"blowoff": [], "low_move": []}
    side_counts: dict[str, int] = {}
    checked: list[dict[str, Any]] = []

    for item in items:
        sym = _pick_symbol(item)
        side = _pick_side(item)
        side_counts[side] = side_counts.get(side, 0) + 1
        if sym and sym in top_symbols and (side == "BUY" or block_sell):
            merged = dict(latest_map.get(sym, {}))
            merged.update(_as_dict(item))
            danger, diag = _is_dangerous_blowoff_row(merged, side=side)
            diag["symbol"] = sym
            diag["detected"] = True
            checked.append(diag)
            if danger:
                skipped["blowoff"].append(sym)
                continue
        kept.append(item)

    logger.warning(
        "[SUMMARY AI PREFILTER] applied before Top3 before=%s after=%s blowoff=%s low_move=%s top_symbols_count=%s source_rows=%s latest_rows=%s block_sell=%s side_counts=%s checked=%s version=%s",
        len(items), len(kept), sorted(set(skipped["blowoff"])), sorted(set(skipped["low_move"])),
        len(top_symbols), source_rows, latest_rows, block_sell, side_counts, checked[:10], VERSION,
    )
    return kept, skipped, top_symbols, {"source_rows": source_rows, "latest_rows": latest_rows, "block_sell": block_sell, "side_counts": side_counts, "checked": checked[:20]}


def _has_any_marker(fn: Any, markers: Sequence[str]) -> bool:
    return any(bool(getattr(fn, m, False)) for m in markers)


def _iter_wrapper_chain(fn: Any, *, limit: int = 32):
    seen: set[int] = set()
    cur = fn
    for _ in range(limit):
        if not callable(cur):
            return
        ident = id(cur)
        if ident in seen:
            return
        seen.add(ident)
        yield cur
        nxt = getattr(cur, "_original", None)
        if not callable(nxt):
            return
        cur = nxt


def _chain_has_marker(fn: Any, markers: Sequence[str]) -> bool:
    return any(_has_any_marker(x, markers) for x in _iter_wrapper_chain(fn))


def _unwrap_own_wrapper(fn: Any) -> Any:
    cur = fn
    seen: set[int] = set()
    for _ in range(32):
        if not callable(cur) or id(cur) in seen:
            return fn
        seen.add(id(cur))
        if not _has_any_marker(cur, _BLOWOFF_MARKERS):
            return cur
        nxt = getattr(cur, "_original", None)
        if not callable(nxt):
            return cur
        cur = nxt
    return fn


def _patch_entry_pipeline_filter(reason: str = "install") -> bool:
    try:
        if not _env_bool("SUMMARY_AI_ENTRY_PIPELINE_BLOWOFF_PATCH_ENABLED", True):
            return True
        import trading.summary.pipeline.entry_pipeline as ep

        cur = getattr(ep, "_filter_blowoff", None)
        if not callable(cur):
            return False
        if _chain_has_marker(cur, _ENTRY_PIPELINE_MARKERS):
            return True

        def patched_filter_blowoff(rows: list[Any], df_summary: pd.DataFrame | None) -> list[Any]:
            try:
                if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty:
                    return rows
                top_symbols, source_rows, latest_rows, latest_map = _detect_blowoff_symbols(df_summary)
                if not top_symbols:
                    return rows
                block_sell = _env_bool("SUMMARY_AI_BLOWOFF_BLOCK_SELL", False)
                filtered: list[Any] = []
                skipped: list[dict[str, Any]] = []
                allowed_detected: list[dict[str, Any]] = []
                for r in rows or []:
                    d = _as_dict(r)
                    symbol = _pick_symbol(d) or _norm_symbol(d.get("symbol"))
                    side = _pick_side(d)
                    if symbol and symbol in top_symbols and (side == "BUY" or block_sell):
                        merged = dict(latest_map.get(symbol, {}))
                        merged.update(d)
                        danger, diag = _is_dangerous_blowoff_row(merged, side=side)
                        diag["symbol"] = symbol
                        if danger:
                            skipped.append(diag)
                            logger.info("[entry_pipeline] skip dangerous blowoff symbol=%s detail=%s version=%s", symbol, diag, VERSION)
                            continue
                        allowed_detected.append(diag)
                    filtered.append(r)
                if skipped or allowed_detected:
                    logger.warning(
                        "[entry_pipeline] Summary-AI blowoff strict filter before=%s after=%s skipped=%s allowed_detected=%s top_symbols_count=%s source_rows=%s latest_rows=%s version=%s",
                        len(rows or []), len(filtered), skipped[:20], allowed_detected[:20], len(top_symbols), source_rows, latest_rows, VERSION,
                    )
                return filtered
            except Exception as e:
                logger.error("[entry_pipeline] patched blowoff filter failed; fail-open version=%s err=%s", VERSION, e)
                return rows

        for marker in _ENTRY_PIPELINE_MARKERS:
            setattr(patched_filter_blowoff, marker, True)
        patched_filter_blowoff._original = cur  # type: ignore[attr-defined]
        ep._filter_blowoff = patched_filter_blowoff
        logger.warning("[SUMMARY AI PREFILTER] patched entry_pipeline._filter_blowoff reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI PREFILTER] entry_pipeline blowoff patch failed reason=%s", reason)
        return False


def _patch_once(reason: str = "install") -> bool:
    try:
        import trading.entry.summary_ai.executor as ex
        try:
            from trading.entry.summary_ai import runner as runner_mod
        except Exception:
            runner_mod = None

        cur = getattr(ex, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI PREFILTER] target missing reason=%s", reason)
            return False
        entry_pipeline_ok = _patch_entry_pipeline_filter(reason=reason)

        # 重要: top だけでなく _original チェーン全体を見て、既に blowoff prefilter が
        # 入っていれば再ラップしない。direct_dispatch/async_entry の watcher と競合して
        # blowoff -> async -> direct -> blowoff の輪を作る事故を防ぐ。
        if _chain_has_marker(cur, _BLOWOFF_MARKERS):
            return bool(entry_pipeline_ok)

        original = _unwrap_own_wrapper(cur)

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
                        "checked": meta.get("checked"),
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

        for marker in _BLOWOFF_MARKERS:
            setattr(patched, marker, True)
        patched._original = original  # type: ignore[attr-defined]
        ex.execute_ai_ok_entries_bulk = patched
        if runner_mod is not None:
            try:
                runner_mod.execute_ai_ok_entries_bulk = patched
            except Exception:
                pass
        logger.warning(
            "[SUMMARY AI PREFILTER] installed reason=%s version=%s block_sell=%s entry_pipeline_ok=%s original=%s",
            reason, VERSION, _env_bool("SUMMARY_AI_BLOWOFF_BLOCK_SELL", False), entry_pipeline_ok, getattr(original, "__name__", type(original).__name__),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI PREFILTER] install failed reason=%s", reason)
        return False


def _watcher() -> None:
    loops = 60
    for i in range(loops):
        try:
            _patch_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[SUMMARY AI PREFILTER] watcher failed", exc_info=True)
        time.sleep(1.0)


def install(reason: str = "manual") -> bool:
    global _INSTALLED, _WATCHER_STARTED
    ok = _patch_once(reason=reason)
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        try:
            threading.Thread(target=_watcher, name="summary-ai-blowoff-prefilter-watch", daemon=True).start()
        except Exception:
            logger.debug("[SUMMARY AI PREFILTER] watcher start failed", exc_info=True)
    logger.warning("[SUMMARY AI PREFILTER] install done ok=%s version=%s", ok, VERSION)
    return bool(ok)


try:
    install(reason="import")
except Exception:
    logger.exception("[SUMMARY AI PREFILTER] auto install failed")

__all__ = ["install", "VERSION"]
