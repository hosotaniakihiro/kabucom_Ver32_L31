# ============================================================
# File   : trading/entry/summary_ai/executor.py
# Version: REV12-INLINE-FRESH-ONE-BY-ONE-TIMEOUT-CONTINUE
# ------------------------------------------------------------
# AI_OK rows -> approved_rows -> entry_pipeline.
#
# REV12:
#   - Also inlined core/startup/summary_ai_direct_timeout_continue_patch.py
#     (V15), discovered mid-merge to be a 5th real patch layered on top of
#     summary_ai_async_direct_dispatch_patch.py's _fallback_direct_dispatch
#     extension point. _dd_fallback_direct_dispatch is now the "fresh
#     one-by-one" version: dispatch batch size is fixed to 1 so a slow
#     board/snapshot call for one symbol cannot delay siblings, a per-candidate
#     timeout only drops that candidate (does not abort the whole retry run),
#     and candidates older than SUMMARY_AI_OK_VALID_SEC are skipped so a stale
#     AI_OK decision never gets dispatched late. The pending-registration
#     freshness guard in that same patch file (wrapping the separately-chained
#     trading.summary.summary_entry.register_pending_entries, which already
#     has 2 unrelated patches of its own) was left as-is -- out of scope for
#     this merge.
#
# REV11:
#   - Inlined the 4 monkeypatches that used to wrap execute_ai_ok_entries_bulk
#     at runtime (core/startup/summary_ai_blowoff_prefilter_patch.py,
#     summary_ai_async_entry_patch.py, summary_ai_async_direct_dispatch_patch.py,
#     summary_ai_low_move_softpass_patch.py's rolling-retry part). The install
#     order in main.py/sitecustomize.py made low_move_softpass's rolling-retry
#     wrapper the outermost layer in production, and it never delegated to the
#     wrapped chain in its success path (only on rolling_retry-disabled/exception
#     fallback) -- so blowoff_prefilter's ai_results filtering (removing
#     dangerous blow-off-top candidates) was silently never applied to real
#     order placement. Fixed by running the blowoff filter unconditionally
#     first, before the rolling-retry logic ever sees ai_results.
#   - _select_ai_ok_items / execute_entry_pipeline / _low_move_hard_block /
#     _range_5m_filter_from_entry_row / _filter_blowoff / _positive_result /
#     _entry_price_bounds remain separate monkeypatch targets (not part of this
#     merge) and are untouched.
#
# REV10:
#   - no-order cleanup now always includes every approved row symbol, even when
#     result contains one attempted symbol. Rolling retry used to prune only the
#     final attempted symbol, leaving prior STRICT_BOARD_MISSING / ATR_NG
#     SUMMARY_AI pending rows in global_data.pending_entries.
#   - Board-missing remains fail-close; this only prevents stale pending buildup.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

from .utils import get_bulk_entry_pipeline, is_market_open, safe_float

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 3
DEFAULT_MIN_BUY_APPROVED = 0
DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY = 7000.0
DEFAULT_MIN_PRICE_FOR_ENTRY = 3000.0
VERSION = "REV12-INLINE-FRESH-ONE-BY-ONE-TIMEOUT-CONTINUE"
_FILTER_WATCHER_STARTED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            ss = s[:-2]
            if ss.isdigit():
                return ss
        return s
    except Exception:
        return ""


def _norm_side(v: Any, default: str = "BUY") -> str:
    try:
        s = str(v or default).strip().upper()
        return s if s in {"BUY", "SELL"} else default
    except Exception:
        return default


def _as_dict(v: Any) -> Dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _dict_sources(item: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return item, ai_row, src


def _pick_symbol(item: Dict[str, Any]) -> str:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return _norm_symbol(item.get("symbol") or ai_row.get("symbol") or src.get("symbol"))


def _pick_side(item: Dict[str, Any]) -> str:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return _norm_side(
        item.get("side") or item.get("ai_side") or ai_row.get("side") or ai_row.get("ai_side")
        or ai_row.get("entry_decision") or src.get("side") or src.get("ai_side") or src.get("entry_decision"),
        "BUY",
    )


def _row_side(item: Dict[str, Any]) -> str:
    return _pick_side(item)


def _pick_first_positive(item: Dict[str, Any], keys: Sequence[str]) -> float:
    for d in _dict_sources(item):
        for k in keys:
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    return 0.0


def _pick_price(item: Dict[str, Any]) -> float:
    return _pick_first_positive(item, ("close_price", "price", "current_price", "close", "last_price", "CurrentPrice"))


def _pick_high(item: Dict[str, Any]) -> float:
    return _pick_first_positive(item, ("high", "high_price", "HighPrice", "day_high"))


def _pick_low(item: Dict[str, Any]) -> float:
    return _pick_first_positive(item, ("low", "low_price", "LowPrice", "day_low"))


def _pick_volume(item: Dict[str, Any]) -> float:
    return _pick_first_positive(item, ("volume", "Volume", "trading_volume", "TradingVolume", "出来高"))


def _pick_turnover(item: Dict[str, Any]) -> float:
    for d in _dict_sources(item):
        for k in ("turnover", "trading_value", "TradingValue", "売買代金"):
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    price = _pick_price(item)
    vol = _pick_volume(item)
    return price * vol if price > 0 and vol > 0 else 0.0


def _pick_range_pct(item: Dict[str, Any]) -> float:
    for d in _dict_sources(item):
        for k in ("range_pct", "intrabar_range_pct", "_intrabar_range_pct"):
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    high = _pick_high(item)
    low = _pick_low(item)
    close = _pick_price(item)
    if close > 0 and high > 0 and low > 0 and high > low:
        return abs(high - low) / close
    return 0.0


def _score_for_side(item: Dict[str, Any]) -> float:
    side = _pick_side(item)
    if side == "SELL":
        return max(safe_float(item.get("sell_score")), abs(safe_float(item.get("score_total"))), abs(safe_float(item.get("final_score"))))
    return max(safe_float(item.get("buy_score")), safe_float(item.get("score_total")), safe_float(item.get("final_score")))


def _row_score_for_side(item: Dict[str, Any]) -> float:
    return _score_for_side(item)


def _sort_key(item: Dict[str, Any]) -> tuple[float, float, float]:
    side = _pick_side(item)
    return (safe_float(item.get("confidence")), _score_for_side(item), safe_float(item.get("sell_score")) if side == "SELL" else safe_float(item.get("buy_score")))


def _price_bounds() -> tuple[float, float, dict[str, Any]]:
    min_price = DEFAULT_MIN_PRICE_FOR_ENTRY
    max_price = DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY
    diag: dict[str, Any] = {"source": "fallback"}
    try:
        from trading.entry.entry_budget import get_entry_min_price, get_entry_max_price, get_effective_entry_max_price, get_max_entry_oneshot_yen, get_order_lot_size
        min_price = float(get_entry_min_price())
        max_price = float(get_effective_entry_max_price() or get_entry_max_price() or max_price)
        diag.update({"source": "entry_budget", "entry_min_price": min_price, "entry_max_price_effective": max_price, "max_oneshot_yen": float(get_max_entry_oneshot_yen()), "lot_size": int(get_order_lot_size())})
    except Exception:
        pass
    min_price = _env_float("SUMMARY_AI_ENTRY_MIN_PRICE", _env_float("ENTRY_MIN_PRICE", min_price))
    max_price = _env_float("SUMMARY_AI_ENTRY_MAX_PRICE_FOR_100_SHARE", _env_float("ENTRY_MAX_PRICE", max_price))
    diag.update({"effective_min_price": min_price, "effective_max_price": max_price})
    return min_price, max_price, diag


def _entry_price_bounds() -> tuple[float, float, dict[str, Any]]:
    return _price_bounds()


def _is_trade_restricted_symbol(symbol: str) -> tuple[bool, Any]:
    try:
        from global_state import global_data
        root = getattr(global_data, "trade_restricted", {}) or {}
        until = root.get(symbol)
        if not until:
            return False, None
        if isinstance(until, dt.datetime) and dt.datetime.now() >= until:
            try:
                root.pop(symbol, None)
            except Exception:
                pass
            return False, None
        return True, until
    except Exception:
        return False, None


def _is_trade_restricted(symbol: str) -> bool:
    return bool(_is_trade_restricted_symbol(symbol)[0])


def _is_sell_reject_cached(symbol: str, side: str) -> tuple[bool, Any]:
    if side != "SELL":
        return False, None
    try:
        from AI.sell_order_reject_cache import is_sell_rejected, get_sell_reject_reason
        if bool(is_sell_rejected(symbol)):
            try:
                return True, get_sell_reject_reason(symbol)
            except Exception:
                return True, "sell_reject_cache"
    except Exception:
        pass
    return False, None


def _daily_risk_block_reason(symbol: str, side: str) -> tuple[bool, str, Dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_PRE_FILTER_DAILY_RISK", True):
        return False, "", {}
    try:
        from core.startup import entry_daily_risk_runtime_patch as daily_risk
        fn = getattr(daily_risk, "_risk_block_reason", None)
        if callable(fn):
            blocked, reason, detail = fn(_norm_symbol(symbol), _norm_side(side, "BUY"))
            if blocked:
                return True, str(reason or "DAILY_RISK_BLOCK"), dict(detail) if isinstance(detail, dict) else {"detail": str(detail)}
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] daily risk compatibility check failed; fail-open", exc_info=True)
    return False, "", {}


def _passes_strict_candidate_quality(item: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_EXECUTOR_STRICT_QUALITY_PREFILTER", True):
        return True, {}
    symbol = _pick_symbol(item)
    side = _pick_side(item)
    range_pct = _pick_range_pct(item)
    min_range_pct = _env_float("SUMMARY_AI_EXECUTOR_MIN_RANGE_PCT", _env_float("SUMMARY_AI_LOW_MOVE_MIN_RANGE_PCT", 0.005))
    if min_range_pct > 0 and range_pct < min_range_pct:
        return False, {
            "symbol": symbol,
            "side": side,
            "reason": "low_move_range_too_small",
            "range_pct": range_pct,
            "min_range_pct": min_range_pct,
            "score": _score_for_side(item),
            "price": _pick_price(item),
            "high": _pick_high(item),
            "low": _pick_low(item),
        }
    return True, {}


def _base_filter_blocked_ai_ok_items(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not ok_items:
        return []
    min_price, max_price, diag = _price_bounds()
    min_volume = _env_float("SUMMARY_AI_EXECUTOR_MIN_VOLUME", _env_float("ENTRY_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("SUMMARY_AI_EXECUTOR_MIN_TURNOVER", _env_float("ENTRY_MIN_TURNOVER", 10000000.0))
    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in ok_items:
        symbol = _pick_symbol(item)
        side = _pick_side(item)
        price = _pick_price(item)
        volume = _pick_volume(item)
        turnover = _pick_turnover(item)
        if price > 0 and min_price > 0 and price < min_price:
            skipped.append({"symbol": symbol, "side": side, "reason": "price_below_min", "price": price})
            continue
        if price > 0 and max_price > 0 and price > max_price:
            skipped.append({"symbol": symbol, "side": side, "reason": "price_over_max", "price": price})
            continue
        if volume > 0 and volume < min_volume:
            skipped.append({"symbol": symbol, "side": side, "reason": "low_volume", "volume": volume, "min_volume": min_volume})
            continue
        if turnover > 0 and turnover < min_turnover:
            skipped.append({"symbol": symbol, "side": side, "reason": "low_turnover", "turnover": turnover, "min_turnover": min_turnover})
            continue
        restricted, until = _is_trade_restricted_symbol(symbol)
        if restricted:
            skipped.append({"symbol": symbol, "side": side, "reason": "trade_restricted", "until": str(until)})
            continue
        sell_cached, sell_reason = _is_sell_reject_cached(symbol, side)
        if sell_cached:
            skipped.append({"symbol": symbol, "side": side, "reason": "sell_reject_cached", "detail": str(sell_reason)})
            continue
        risk_blocked, risk_reason, risk_detail = _daily_risk_block_reason(symbol, side)
        if risk_blocked:
            skipped.append({"symbol": symbol, "side": side, "reason": risk_reason, "detail": risk_detail})
            continue
        quality_ok, quality_detail = _passes_strict_candidate_quality(item)
        if not quality_ok:
            skipped.append(quality_detail)
            continue
        kept.append(item)
    if skipped:
        logger.warning("[SUMMARY AI EXECUTOR] strict prefilter skipped=%s kept=%s diag=%s sample=%s version=%s", len(skipped), len(kept), diag, skipped[:10], VERSION)
    return kept


_ORIGINAL_FILTER_BLOCKED_AI_OK_ITEMS = _base_filter_blocked_ai_ok_items
_filter_blocked_ai_ok_items = _base_filter_blocked_ai_ok_items


def _ensure_core_filter(reason: str = "") -> bool:
    global _filter_blocked_ai_ok_items
    if _filter_blocked_ai_ok_items is not _ORIGINAL_FILTER_BLOCKED_AI_OK_ITEMS:
        _filter_blocked_ai_ok_items = _ORIGINAL_FILTER_BLOCKED_AI_OK_ITEMS
        logger.warning("[SUMMARY AI EXECUTOR] core filter restored reason=%s version=%s", reason, VERSION)
    return True


def _start_filter_watcher() -> None:
    global _FILTER_WATCHER_STARTED
    if _FILTER_WATCHER_STARTED or not _env_bool("SUMMARY_AI_PROTECT_CORE_FILTER", True):
        return
    _FILTER_WATCHER_STARTED = True

    def _loop() -> None:
        for i in range(180):
            try:
                _ensure_core_filter(reason=f"watcher:{i}")
            except Exception:
                pass
            time.sleep(0.5)

    try:
        threading.Thread(target=_loop, name="summary-ai-core-filter-watch", daemon=True).start()
        logger.warning("[SUMMARY AI EXECUTOR] core filter watcher started version=%s", VERSION)
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] core filter watcher start failed", exc_info=True)


def _effective_max_entries(max_entries: int) -> int:
    try:
        requested = int(max_entries or DEFAULT_MAX_ENTRIES)
    except Exception:
        requested = DEFAULT_MAX_ENTRIES
    hard_cap = _env_int("SUMMARY_AI_MAX_REAL_ENTRIES", DEFAULT_MAX_ENTRIES)
    return max(1, min(requested, hard_cap, 3))


def _selected_pool(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    kept = _base_filter_blocked_ai_ok_items(ok_items)
    if not kept:
        logger.warning("[SUMMARY AI EXECUTOR] no AI_OK rows after strict prefilter ok_total=%s version=%s", len(ok_items or []), VERSION)
        return []
    hard_cap = _effective_max_entries(max_entries)
    pool_n = max(hard_cap, _env_int("SUMMARY_AI_ROLLING_RETRY_POOL", _env_int("SUMMARY_AI_EXECUTOR_SELECTION_POOL", 20)))
    pool_n = min(max(1, pool_n), len(kept))
    return sorted(kept, key=_sort_key, reverse=True)[:pool_n]


def _select_ai_ok_items(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    _ensure_core_filter(reason="select")
    pool = _selected_pool(ok_items, max_entries=max_entries)
    selected = pool[:_effective_max_entries(max_entries)]
    logger.warning("[SUMMARY AI EXECUTOR] top selection requested=%s cap=%s pool=%s ok_total=%s selected=%s version=%s", max_entries, _effective_max_entries(max_entries), len(pool), len(ok_items or []), [{"symbol": _pick_symbol(x), "side": _pick_side(x), "price": _pick_price(x), "conf": round(safe_float(x.get("confidence")), 3), "score": round(_score_for_side(x), 3), "range_pct": round(_pick_range_pct(x), 6)} for x in selected], VERSION)
    return selected


def _iter_dicts_deep(obj: Any, *, depth: int = 0) -> Iterable[Dict[str, Any]]:
    if depth > 5:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts_deep(v, depth=depth + 1)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _iter_dicts_deep(v, depth=depth + 1)


def _has_real_order_evidence(result: Any) -> bool:
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for d in _iter_dicts_deep(result):
            for k in ("order_id", "OrderId", "order_no", "OrderNo", "execution_id", "ExecutionID"):
                if d.get(k):
                    return True
            for k in ("orders", "order_ids", "sent_orders", "submitted_orders", "accepted_orders", "executed_symbols"):
                v = d.get(k)
                if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                    return True
            for k in ("order_sent", "order_submitted", "entry_executed"):
                if bool(d.get(k)):
                    return True
        return bool(result.get("executed"))
    if isinstance(result, (list, tuple, set)):
        return len(result) > 0
    return bool(result)


def _positive_result(result: Any) -> bool:
    return _has_real_order_evidence(result)


def _collect_symbols_for_pending_cleanup(result: Any, approved_rows: Sequence[Dict[str, Any]]) -> Set[str]:
    symbols: Set[str] = set()
    try:
        for d in _iter_dicts_deep(result):
            for key in ("symbol", "Symbol", "銘柄コード", "code", "stock_code"):
                sym = _norm_symbol(d.get(key))
                if sym:
                    symbols.add(sym)
            pending_root = d.get("pending_root")
            if isinstance(pending_root, dict):
                for sym in pending_root.keys():
                    ss = _norm_symbol(sym)
                    if ss:
                        symbols.add(ss)
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] collect cleanup symbols from result failed", exc_info=True)
    # REV10: always include all approved symbols. Before REV10 this ran only
    # when result had no symbol, so rolling retry pruned only the final attempted
    # symbol and left prior STRICT_BOARD_MISSING pending rows behind.
    for row in approved_rows or []:
        if isinstance(row, dict):
            sym = _norm_symbol(row.get("symbol") or row.get("Symbol"))
            if sym:
                symbols.add(sym)
    return symbols


def _cleanup_pending_after_no_order(result: Any, approved_rows: Sequence[Dict[str, Any]], *, reason: str) -> int:
    if not _env_bool("SUMMARY_AI_CLEAN_PENDING_ON_NO_ORDER", True):
        return 0
    symbols = _collect_symbols_for_pending_cleanup(result, approved_rows)
    if not symbols:
        return 0
    try:
        from trading.entry.pending_manager import prune_entries, snapshot_root
        def _predicate(sym: str, entry: Dict[str, Any]) -> bool:
            if _norm_symbol(sym) not in symbols:
                return False
            source = str(entry.get("source") or "").strip().upper()
            entry_type = str(entry.get("entry_type") or "").strip().upper()
            return entry_type == "SUMMARY_AI" or source in {"SUMMARY", "SUMMARY_AI", "PUSH", "PUSH_SUMMARY"}
        removed = int(prune_entries(_predicate, reason=f"SUMMARY_AI_NO_ORDER:{reason}"))
        logger.warning("[SUMMARY AI EXECUTOR] pending cleanup after no-order reason=%s symbols=%s removed=%s root=%s result=%s version=%s", reason, sorted(symbols), removed, snapshot_root(), _summarize_no_order_result(result), VERSION)
        return removed
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] pending cleanup after no-order failed reason=%s symbols=%s", reason, sorted(symbols))
        return 0


def _summarize_no_order_result(result: Any) -> Any:
    try:
        if not isinstance(result, dict):
            return result
        summary: Dict[str, Any] = {}
        for k in ("executed", "entries", "attempted_count", "approved", "registered", "skip_reason", "reason", "error", "pending_root", "skipped"):
            if k in result:
                summary[k] = result.get(k)
        nested = result.get("result") or result.get("pipeline_result")
        if isinstance(nested, dict):
            summary["nested"] = {k: nested.get(k) for k in ("executed", "entries", "attempted_count", "approved", "registered", "skip_reason", "reason", "error", "pending_root", "skipped") if k in nested}
        return summary or result
    except Exception:
        return str(result)


def _retryable_no_tradable(result: Any) -> bool:
    try:
        if not isinstance(result, dict):
            return False
        texts: list[str] = []
        for d in _iter_dicts_deep(result):
            for k in ("skip_reason", "reason", "error"):
                v = d.get(k)
                if v:
                    texts.append(str(v).lower())
            skipped = d.get("skipped")
            if isinstance(skipped, dict) and sum(int(v or 0) for v in skipped.values() if isinstance(v, (int, float))) > 0:
                texts.append("filtered")
        s = "|".join(texts)
        return any(x in s for x in ("no_tradable_rows_after_filters", "filtered", "entry_pipeline_no_order"))
    except Exception:
        return False


def build_approved_row(ai_ok_item: Dict[str, Any]) -> Dict[str, Any]:
    ai_row = _as_dict(ai_ok_item.get("ai_row"))
    src = _as_dict(ai_ok_item.get("source_row"))
    side = _pick_side(ai_ok_item)
    price = ai_row.get("close_price") or ai_row.get("price") or src.get("close") or src.get("price") or ai_ok_item.get("price")
    buy_score = ai_row.get("buy_score", ai_ok_item.get("buy_score"))
    sell_score = ai_row.get("sell_score", ai_ok_item.get("sell_score"))
    score_total = ai_row.get("score_total", ai_ok_item.get("score_total"))
    final_score = ai_row.get("final_score", ai_ok_item.get("final_score"))
    row = dict(src)
    row.update({
        "symbol": ai_ok_item.get("symbol") or ai_row.get("symbol") or src.get("symbol"),
        "symbolname": ai_ok_item.get("symbolname") or ai_row.get("symbolname") or src.get("symbolname"),
        "side": side,
        "ai_side": side,
        "entry_decision": side,
        "source": ai_row.get("source", src.get("source", "SUMMARY")),
        "interval": ai_row.get("interval", src.get("interval", 1)),
        "price": price,
        "close_price": price,
        "close": price,
        "confidence": ai_ok_item.get("confidence", 0.0),
        "ai_confidence": ai_ok_item.get("confidence", 0.0),
        "lot_multiplier": ai_ok_item.get("lot_multiplier", 1.0),
        "ai_reason": ai_ok_item.get("reason", ""),
        "reason": ai_ok_item.get("reason", ""),
        "model_used": ai_ok_item.get("model_used", ""),
        "score_total": score_total,
        "total_score": score_total,
        "score": score_total,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "score_buy": buy_score,
        "score_sell": sell_score,
        "final_score": final_score,
        "display_score": final_score,
        "turnover": ai_row.get("turnover", src.get("turnover") or src.get("trading_value")),
        "volume": ai_row.get("volume", src.get("volume")),
        "datetime": ai_row.get("datetime", src.get("datetime")),
        "entry_type": ai_row.get("entry_type") or src.get("entry_type") or "SUMMARY_AI",
        "ai_gate_allow": True,
    })
    logger.info("[SUMMARY AI EXECUTOR] approved row built symbol=%s side=%s conf=%.3f total=%.3f close=%s range_pct=%.6f version=%s", row.get("symbol"), side, safe_float(row.get("ai_confidence")), safe_float(row.get("score_total")), row.get("close_price"), _pick_range_pct(row), VERSION)
    return row


def build_ai_ok_approved_rows(ai_results: Sequence[Dict[str, Any]], *, max_entries: int = DEFAULT_MAX_ENTRIES) -> List[Dict[str, Any]]:
    _ensure_core_filter(reason="approved_build")
    ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
    approved = [build_approved_row(x) for x in _select_ai_ok_items(ok_items, max_entries=max_entries)]
    logger.info("[SUMMARY AI EXECUTOR] approved selection max_entries=%s rows=%s version=%s", max_entries, len(approved), VERSION)
    return approved


# ============================================================
# blow-off top prefilter (ported from
# core/startup/summary_ai_blowoff_prefilter_patch.py V9)
# ============================================================

def _blowoff_latest_rows_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
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


def _blowoff_latest_row_map(df_summary: Any) -> Dict[str, Dict[str, Any]]:
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty or "symbol" not in df_summary.columns:
            return {}
        latest = _blowoff_latest_rows_per_symbol(df_summary)
        if latest is None or not isinstance(latest, pd.DataFrame) or latest.empty:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for _, row in latest.iterrows():
            d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
            sym = _norm_symbol(d.get("symbol"))
            if sym:
                out[sym] = d
        return out
    except Exception:
        logger.debug("[SUMMARY AI PREFILTER] latest row map failed", exc_info=True)
        return {}


def _blowoff_extract_symbols_from_tops(tops: Any) -> Set[str]:
    try:
        if tops is None or not isinstance(tops, pd.DataFrame) or tops.empty or "symbol" not in tops.columns:
            return set()
        return {_norm_symbol(x) for x in tops["symbol"].dropna().astype(str).tolist() if _norm_symbol(x)}
    except Exception:
        return set()


def _blowoff_range_pct_value(row: Dict[str, Any], *, close: float, high: float, low: float) -> float:
    raw = 0.0
    for key in ("range_pct", "intrabar_range_pct", "_intrabar_range_pct"):
        raw = safe_float(row.get(key), 0.0)
        if raw > 0:
            break
    if raw > 0:
        return raw * 100.0 if raw <= 0.5 else raw
    base = close if close > 0 else max(high, low, 1.0)
    if high > 0 and low > 0 and high > low and base > 0:
        return abs(high - low) / base * 100.0
    return 0.0


def _blowoff_close_position(row: Dict[str, Any], *, close: float, high: float, low: float) -> float:
    if high > low and close > 0:
        return max(0.0, min(1.0, (close - low) / max(high - low, 1e-9)))
    return 0.5


def _is_dangerous_blowoff_row(row: Dict[str, Any], *, side: str) -> Tuple[bool, Dict[str, Any]]:
    close = safe_float(row.get("close") or row.get("close_price") or row.get("price") or row.get("current_price"), 0.0)
    high = safe_float(row.get("high") or row.get("high_price") or row.get("day_high"), 0.0)
    low = safe_float(row.get("low") or row.get("low_price") or row.get("day_low"), 0.0)
    rsi = safe_float(row.get("rsi"), 50.0)
    slope = safe_float(row.get("slope_atr_scaled") or row.get("slope") or row.get("score_slope"), 0.0)
    range_pct = _blowoff_range_pct_value(row, close=close, high=high, low=low)
    close_pos = _blowoff_close_position(row, close=close, high=high, low=low)
    min_rsi = _env_float("SUMMARY_AI_BLOWOFF_DANGER_RSI", 80.0)
    min_range = _env_float("SUMMARY_AI_BLOWOFF_DANGER_RANGE_PCT", 2.5)
    min_close_pos = _env_float("SUMMARY_AI_BLOWOFF_DANGER_CLOSE_POS", 0.85)

    side = _norm_side(side, "BUY")
    if side == "BUY":
        ok = rsi >= min_rsi and range_pct >= min_range and slope > 0 and close_pos >= min_close_pos
    else:
        ok = rsi <= (100.0 - min_rsi) and range_pct >= min_range and slope < 0 and close_pos <= (1.0 - min_close_pos)
    return bool(ok), {
        "side": side, "rsi": round(rsi, 3), "range_pct": round(range_pct, 3), "slope": round(slope, 6),
        "close_pos": round(close_pos, 3), "close": close, "high": high, "low": low,
        "need_rsi": min_rsi, "need_range_pct": min_range, "need_close_pos": min_close_pos,
    }


def _detect_blowoff_symbols(df_summary: Any) -> Tuple[Set[str], int, int, Dict[str, Dict[str, Any]]]:
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty:
            return set(), 0, 0, {}
        from trading.ai.blowoff_top_detector import detect_blowoff_top

        source_rows = len(df_summary)
        latest_df = _blowoff_latest_rows_per_symbol(df_summary)
        latest_rows = len(latest_df) if isinstance(latest_df, pd.DataFrame) else 0
        latest_map = _blowoff_latest_row_map(latest_df)

        top_symbols = _blowoff_extract_symbols_from_tops(detect_blowoff_top(df_summary))
        if not top_symbols:
            top_symbols = _blowoff_extract_symbols_from_tops(detect_blowoff_top(latest_df))
        return top_symbols, source_rows, latest_rows, latest_map
    except Exception as e:
        # re-raising via logger.exception here would recurse on formatting; keep fail-open with logger.error.
        logger.error("[SUMMARY AI PREFILTER] blowoff detect failed; fail-open err=%s", e)
        return set(), 0, 0, {}


def _blowoff_filter_ai_results(ai_results: Any, df_summary: Any) -> Tuple[List[Any], Dict[str, List[str]], Set[str], Dict[str, Any]]:
    items = list(ai_results or [])
    top_symbols, source_rows, latest_rows, latest_map = _detect_blowoff_symbols(df_summary) if _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True) else (set(), 0, 0, {})
    block_sell = _env_bool("SUMMARY_AI_BLOWOFF_BLOCK_SELL", False)
    kept: List[Any] = []
    skipped: Dict[str, List[str]] = {"blowoff": [], "low_move": []}
    side_counts: Dict[str, int] = {}
    checked: List[Dict[str, Any]] = []

    for item in items:
        sym = _pick_symbol(item) if isinstance(item, dict) else ""
        side = _pick_side(item) if isinstance(item, dict) else "BUY"
        side_counts[side] = side_counts.get(side, 0) + 1
        if sym and sym in top_symbols and (side == "BUY" or block_sell):
            merged = dict(latest_map.get(sym, {}))
            merged.update(item if isinstance(item, dict) else {})
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


def _attach_blowoff_diagnostics(result: Any, *, before_n: int, after_n: int, skipped: Dict[str, List[str]], top_symbols: Set[str], meta: Dict[str, Any]) -> Any:
    try:
        if isinstance(result, dict):
            pre = {
                "before": before_n, "after": after_n,
                "skipped": {k: sorted(set(v)) for k, v in skipped.items()},
                "top_symbols_count": len(top_symbols),
                "source_rows": meta.get("source_rows"), "latest_rows": meta.get("latest_rows"),
                "block_sell": meta.get("block_sell"), "side_counts": meta.get("side_counts"),
                "checked": meta.get("checked"), "version": VERSION,
            }
            result["summary_ai_prefilter"] = pre
            result["blowoff_prefilter"] = pre
            if after_n == 0 and before_n > 0:
                result["skip_reason"] = "summary_ai_prefilter_all_blocked"
    except Exception:
        pass
    return result


# ============================================================
# direct-dispatch rolling snapshot fallback (ported from
# core/startup/summary_ai_async_direct_dispatch_patch.py V11)
# ============================================================

_DD_RETRYABLE_NO_ORDER_MARKERS = (
    "queued_async", "snapshot_no_order", "entry_controller_no_order", "summary_entry_executor_no_order",
    "entry_pipeline_no_order", "pending_moved_without_order", "order_id_empty_retryable",
    "entry_controller_lock_timeout", "pipeline_busy", "already_running", "no_pending_registered",
    "pipeline_filter_mismatch", "no_tradable_rows_after_filters",
)


def _dd_summary_ai_price_floor() -> float:
    return max(0.0, _env_float("SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE", 2500.0))


def _dd_force_direct_sync_env() -> None:
    os.environ["SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"] = "1"
    os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", "2")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", "0.7")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_PIPELINE_SOURCE", "SUMMARY")
    os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", "8.0")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SCAN_LIMIT", "12")
    # one-by-one dispatch (batch size fixed to 1) so a slow board/snapshot call
    # for one symbol cannot delay or block sibling candidates in the same batch.
    os.environ["SUMMARY_AI_DIRECT_DISPATCH_BATCH_SIZE"] = "1"
    os.environ.setdefault("SUMMARY_AI_DIRECT_FRESH_MAX_ATTEMPTS", "1")
    os.environ.setdefault("SUMMARY_AI_OK_VALID_SEC", "12.0")
    os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_FRESH_TIMEOUT_SEC", "2.0")
    floor = _dd_summary_ai_price_floor()
    if floor > 0:
        os.environ["SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE"] = str(floor)
        os.environ["SUMMARY_AI_ENTRY_MIN_PRICE"] = str(floor)
        os.environ["ENTRY_MIN_PRICE"] = str(floor)
        os.environ["SUMMARY_AI_LIQ_MIN_PRICE"] = str(floor)


def _dd_resolve_pipeline_source(rows: List[Any]) -> str:
    try:
        counts: Dict[str, int] = {}
        for r in list(rows or [])[:50]:
            d = _as_dict(r)
            nested_ai = _as_dict(d.get("ai"))
            nested_entry = _as_dict(d.get("entry"))
            nested_row = _as_dict(d.get("entry_row"))
            vals = [
                d.get("pipeline_source"), d.get("source"),
                nested_entry.get("pipeline_source"), nested_entry.get("source"),
                nested_row.get("pipeline_source"), nested_row.get("source"),
                nested_ai.get("pipeline_source"), nested_ai.get("source"),
                d.get("entry_type"), nested_entry.get("entry_type"), nested_ai.get("entry_type"),
            ]
            for v in vals:
                s = str(v or "").strip().upper()
                if not s:
                    continue
                if s in {"SUMMARY_AI", "AI"}:
                    continue
                if s in {"PUSH", "SUMMARY"}:
                    s = "SUMMARY"
                if s in {"SUMMARY", "RANKING", "TONOSAMA"}:
                    counts[s] = counts.get(s, 0) + 1
                    break
        if counts:
            return max(counts.items(), key=lambda kv: kv[1])[0]
    except Exception:
        logger.debug("[SUMMARY AI DIRECT DISPATCH] pipeline_source resolve failed", exc_info=True)
    return os.getenv("SUMMARY_AI_DIRECT_DISPATCH_PIPELINE_SOURCE", "SUMMARY").strip().upper() or "SUMMARY"


def _dd_flatten_reasons(result: Any) -> str:
    reasons: List[str] = []
    seen: Set[int] = set()

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        try:
            oid = id(v)
            if oid in seen:
                return
            seen.add(oid)
        except Exception:
            pass
        if isinstance(v, dict):
            for k in ("skip_reason", "reason", "status", "lock_wait_reason"):
                r = v.get(k)
                if r:
                    reasons.append(str(r))
            for k in ("result", "pipeline_result", "direct_dispatch_result"):
                child = v.get(k)
                if child is not None and child is not v:
                    walk(child, depth + 1)
            if isinstance(v.get("result"), list):
                walk(v.get("result"), depth + 1)
        elif isinstance(v, (list, tuple, set)):
            for x in list(v)[:30]:
                walk(x, depth + 1)

    walk(result)
    return "|".join(reasons)


def _dd_strict_result_executed(result: Any) -> bool:
    """True only when an order/entry was actually submitted/executed."""
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return bool(result)
        if isinstance(result, dict):
            if result.get("executed") is False:
                return False
            for key in ("executed", "order_sent", "order_submitted", "success", "entry_executed"):
                if bool(result.get(key)):
                    return True
            for key in ("executed_count", "order_count", "submitted_count", "sent_count", "entries"):
                if int(safe_float(result.get(key), 0)) > 0:
                    return True
            for key in ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols"):
                v = result.get(key)
                if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                    return True
                if v and not isinstance(v, (list, tuple, set, dict)):
                    return True
            for key in ("result", "pipeline_result", "direct_dispatch_result"):
                child = result.get(key)
                if child is not result and _dd_strict_result_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_dd_strict_result_executed(x) for x in result)
        return False
    except Exception:
        return False


def _dd_is_queued_async(result: Any) -> bool:
    try:
        if not isinstance(result, dict):
            return False
        if bool(result.get("submitted_async")) or bool(result.get("queued_async")):
            return True
        child = result.get("result")
        if isinstance(child, dict) and str(child.get("status") or "").lower() == "queued_async":
            return True
    except Exception:
        pass
    return False


def _dd_registered_count(result: Any) -> int:
    try:
        if isinstance(result, dict):
            direct = result.get("registered")
            if direct is not None:
                return int(safe_float(direct, 0))
            for key in ("result", "pipeline_result", "direct_dispatch_result"):
                n = _dd_registered_count(result.get(key))
                if n > 0:
                    return n
    except Exception:
        pass
    return 0


def _dd_is_retryable_no_order(result: Any) -> bool:
    try:
        if _dd_strict_result_executed(result):
            return False
        text = _dd_flatten_reasons(result).lower()
        if any(x in text for x in _DD_RETRYABLE_NO_ORDER_MARKERS):
            return True
        if _dd_registered_count(result) > 0:
            return True
    except Exception:
        pass
    return False


def _dd_is_timeout_result(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("timeout"))


def _dd_call_with_timeout(label: str, rows: List[Any], timeout_sec: float, fn: Callable[[], Any]) -> Any:
    timeout_sec = float(timeout_sec or 0.0)
    if timeout_sec <= 0:
        return fn()
    box: Dict[str, Any] = {"done": False, "result": None, "error": None}

    def _target() -> None:
        try:
            box["result"] = fn()
        except Exception as e:
            box["error"] = e
        finally:
            box["done"] = True

    symbols = [str(_pick_symbol(r)) for r in list(rows or [])[:20] if isinstance(r, dict)]
    started = time.time()
    th = threading.Thread(target=_target, daemon=True, name=f"summary-ai-direct-{label}")
    th.start()
    th.join(timeout_sec)
    elapsed = time.time() - started
    if th.is_alive():
        logger.error(
            "[SUMMARY AI DIRECT DISPATCH] %s timeout timeout=%.3fs elapsed=%.3fs symbols=%s version=%s note=inner_thread_left_daemon_to_avoid_blocking",
            label, timeout_sec, elapsed, symbols, VERSION,
        )
        return {"executed": False, "timeout": True, "skip_reason": f"{label}_timeout", "elapsed_sec": elapsed, "symbols": symbols}
    if box.get("error") is not None:
        raise box["error"]
    return box.get("result")


def _dd_direct_snapshot_execute(approved_rows: List[Any], interval: Any) -> Any:
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True):
        return None
    try:
        from trading.summary import summary_entry as se
        fn = getattr(se, "execute_entry_pipeline", None)
        if not callable(fn):
            return None
        pipeline_source = _dd_resolve_pipeline_source(approved_rows)
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] direct snapshot pipeline_source resolved=%s timeout=%.3fs price_floor=%.0f version=%s",
            pipeline_source, _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0), _dd_summary_ai_price_floor(), VERSION,
        )
        return fn(approved_rows, pipeline_source=pipeline_source, interval=interval)
    except TypeError:
        try:
            from trading.summary import summary_entry as se
            fn = getattr(se, "execute_entry_pipeline", None)
            if callable(fn):
                return fn(approved_rows)
        except Exception:
            logger.exception("[SUMMARY AI DIRECT DISPATCH] direct snapshot fallback failed")
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] direct snapshot failed")
    return None


def _dd_batch_size() -> int:
    return max(1, min(_env_int("SUMMARY_AI_DIRECT_DISPATCH_BATCH_SIZE", 3), 3))


def _dd_rows_from_result(result: Any) -> List[Any]:
    try:
        if isinstance(result, dict):
            rows = result.get("approved_rows")
            if isinstance(rows, list):
                return list(rows)
            rows = result.get("entries")
            if isinstance(rows, list):
                return list(rows)
            for key in ("result", "pipeline_result"):
                child = result.get(key)
                rows = _dd_rows_from_result(child)
                if rows:
                    return rows
    except Exception:
        pass
    return []


def _dd_build_rolling_rows_from_ai_results(ai_results: Any, existing_rows: List[Any]) -> List[Any]:
    """Build additional approved rows from AI_OK candidates. Final guards are not bypassed."""
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", True):
        return []
    try:
        existing_symbols = {str(_pick_symbol(r)) for r in list(existing_rows or []) if isinstance(r, dict)}
        ok_items = [x for x in list(ai_results or []) if isinstance(x, dict) and bool(x.get("allow"))]
        try:
            kept = _filter_blocked_ai_ok_items(ok_items)
        except Exception:
            kept = ok_items
        try:
            ordered = sorted(kept, key=_sort_key, reverse=True)
        except Exception:
            ordered = kept
        scan_limit = max(_dd_batch_size(), _env_int("SUMMARY_AI_DIRECT_DISPATCH_SCAN_LIMIT", 12))
        rows: List[Any] = []
        for item in ordered[:scan_limit]:
            sym = str(item.get("symbol") or "").strip()
            if not sym or sym in existing_symbols:
                continue
            try:
                row = build_approved_row(item)
            except Exception:
                logger.debug("[SUMMARY AI DIRECT DISPATCH] build approved row failed symbol=%s", sym, exc_info=True)
                continue
            if row:
                rows.append(row)
                existing_symbols.add(sym)
        if rows:
            logger.warning("[SUMMARY AI DIRECT DISPATCH] rolling extra approved rows built existing=%s extra=%s version=%s", len(existing_rows or []), len(rows), VERSION)
        return rows
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] rolling row build failed")
        return []


# ---- fresh one-by-one timeout-continue state (ported from
# core/startup/summary_ai_direct_timeout_continue_patch.py V15) ----
# symbol -> {approved_at_ts, valid_until_ts, max_age_sec}
_DD_FRESHNESS_BY_SYMBOL: Dict[str, Dict[str, float]] = {}

_DD_NO_RETRY_SAME_ROW_MARKERS = (
    "entry_pipeline_no_order", "no_tradable_rows_after_filters", "blowoff",
    "liquidity", "sell_credit", "position", "low_move", "range_atr",
)


def _dd_should_skip_original_rows(result: Any) -> bool:
    try:
        reason = str(_dd_flatten_reasons(result) or "").lower()
        return any(x in reason for x in _DD_NO_RETRY_SAME_ROW_MARKERS)
    except Exception:
        return False


def _dd_dedupe_rows(rows: List[Any]) -> List[Any]:
    out: List[Any] = []
    seen: Set[str] = set()
    try:
        for r in list(rows or []):
            try:
                sym = str(_pick_symbol(r) or "").strip()
            except Exception:
                sym = ""
            key = sym or str(id(r))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    except Exception:
        return list(rows or [])
    return out


def _dd_row_to_mutable_dict(row: Any) -> Optional[Dict[str, Any]]:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        return None
    return None


def _dd_mark_rows_fresh(rows: List[Any], *, approved_at_ts: float, max_age_sec: float) -> List[Any]:
    out: List[Any] = []
    valid_until_ts = float(approved_at_ts) + max(0.1, float(max_age_sec or 0.0))
    try:
        now = time.time()
        for sym, meta in list(_DD_FRESHNESS_BY_SYMBOL.items()):
            try:
                if float(meta.get("valid_until_ts") or 0.0) < now - 30.0:
                    _DD_FRESHNESS_BY_SYMBOL.pop(sym, None)
            except Exception:
                _DD_FRESHNESS_BY_SYMBOL.pop(sym, None)

        for row in list(rows or []):
            try:
                sym = str(_pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            if sym:
                _DD_FRESHNESS_BY_SYMBOL[sym] = {
                    "approved_at_ts": float(approved_at_ts),
                    "valid_until_ts": float(valid_until_ts),
                    "max_age_sec": float(max_age_sec),
                }
            d = _dd_row_to_mutable_dict(row)
            if d is not None:
                d.setdefault("summary_ai_approved_at_ts", float(approved_at_ts))
                d.setdefault("summary_ai_valid_until_ts", float(valid_until_ts))
                d.setdefault("summary_ai_max_age_sec", float(max_age_sec))
            out.append(row)
    except Exception:
        logger.debug("[SUMMARY AI DIRECT FRESHNESS] mark rows failed", exc_info=True)
        return list(rows or [])
    return out


def _dd_is_row_fresh(row: Any, *, now_ts: Optional[float] = None) -> bool:
    try:
        now = float(now_ts if now_ts is not None else time.time())
        d = _dd_row_to_mutable_dict(row) or {}
        valid_until = d.get("summary_ai_valid_until_ts")
        if valid_until is None:
            try:
                sym = str(_pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            meta = _DD_FRESHNESS_BY_SYMBOL.get(sym) if sym else None
            if meta:
                valid_until = meta.get("valid_until_ts")
        if valid_until is None:
            return True
        return now <= float(valid_until)
    except Exception:
        return True


def _dd_filter_fresh_rows(rows: List[Any]) -> Tuple[List[Any], List[str]]:
    fresh: List[Any] = []
    stale_symbols: List[str] = []
    now = time.time()
    for row in list(rows or []):
        if _dd_is_row_fresh(row, now_ts=now):
            fresh.append(row)
        else:
            try:
                sym = str(_pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            stale_symbols.append(sym or "?")
    return fresh, stale_symbols


def _dd_fallback_direct_dispatch(result: Any, kwargs: Dict[str, Any]) -> Any:
    """Rolling, one-by-one, freshness-aware retry across AI_OK candidates.

    A per-candidate timeout only drops that candidate (does not abort the
    whole batch), and a candidate is skipped once it's older than
    SUMMARY_AI_OK_VALID_SEC so a slow retry chain never dispatches a stale
    AI_OK decision late.
    """
    try:
        if _dd_strict_result_executed(result):
            return result
        if not (_dd_is_queued_async(result) or _dd_is_retryable_no_order(result)):
            return result
        approved_rows = _dd_rows_from_result(result)
        if not approved_rows:
            return result

        ai_results = kwargs.get("ai_results")
        extra_rows = _dd_build_rolling_rows_from_ai_results(ai_results, approved_rows)
        skip_original = _dd_should_skip_original_rows(result)
        candidate_rows = list(extra_rows) if skip_original else (list(approved_rows) + list(extra_rows))
        candidate_rows = _dd_dedupe_rows(candidate_rows)
        if not candidate_rows:
            return result

        interval = kwargs.get("interval", 1)
        approved_at_ts = time.time()
        max_age_sec = max(1.0, _env_float("SUMMARY_AI_OK_VALID_SEC", 12.0))
        candidate_rows = _dd_mark_rows_fresh(candidate_rows, approved_at_ts=approved_at_ts, max_age_sec=max_age_sec)
        candidate_rows, stale_symbols = _dd_filter_fresh_rows(candidate_rows)
        if not candidate_rows:
            if isinstance(result, dict):
                out = dict(result)
                out["executed"] = False
                out["skip_reason"] = "summary_ai_ok_expired_before_dispatch"
                out["summary_ai_expired_symbols"] = stale_symbols
                out["summary_ai_ok_valid_sec"] = max_age_sec
                return out
            return result

        attempts = max(1, _env_int("SUMMARY_AI_DIRECT_FRESH_MAX_ATTEMPTS", 1))
        retry_sleep = max(0.1, _env_float("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", 0.3))
        configured_timeout = _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0)
        fresh_timeout = _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_FRESH_TIMEOUT_SEC", 2.0)
        timeout_sec = max(0.3, min(float(configured_timeout), float(fresh_timeout), float(max_age_sec)))

        batches = [[r] for r in candidate_rows]
        last_result: Any = None
        timeout_seen = False
        freshness_expired_seen = False
        attempt_records: List[Dict[str, Any]] = []

        for batch_idx, batch in enumerate(batches, start=1):
            fresh_batch, stale_batch_symbols = _dd_filter_fresh_rows(batch)
            if stale_batch_symbols:
                freshness_expired_seen = True
                attempt_records.append({
                    "batch": batch_idx, "attempt": 0, "symbols": stale_batch_symbols,
                    "executed": False, "timeout": False, "retryable": False,
                    "reason_chain": "summary_ai_ok_expired_before_snapshot",
                })
            if not fresh_batch:
                continue

            for attempt in range(1, attempts + 1):
                if time.time() > approved_at_ts + max_age_sec:
                    freshness_expired_seen = True
                    break

                started = time.time()
                snap_result = _dd_call_with_timeout("direct_snapshot", fresh_batch, timeout_sec, lambda b=fresh_batch: _dd_direct_snapshot_execute(b, interval))
                last_result = snap_result
                executed = _dd_strict_result_executed(snap_result)
                timeout = _dd_is_timeout_result(snap_result)
                retryable = _dd_is_retryable_no_order(snap_result)
                timeout_seen = bool(timeout_seen or timeout)
                attempt_records.append({
                    "batch": batch_idx, "attempt": attempt, "executed": executed, "timeout": timeout,
                    "retryable": retryable, "reason_chain": _dd_flatten_reasons(snap_result),
                })
                logger.warning(
                    "[SUMMARY AI DIRECT DISPATCH] fresh one-by-one snapshot done batch=%s/%s attempt=%s/%s elapsed=%.3fs executed=%s timeout=%s retryable=%s reason_chain=%s version=%s",
                    batch_idx, len(batches), attempt, attempts, time.time() - started, executed, timeout, retryable, _dd_flatten_reasons(snap_result), VERSION,
                )
                if executed:
                    break
                if timeout:
                    # timeoutした銘柄だけ捨てる。fresh window内なら次のAI_OK候補へ進む。
                    break
                if not retryable:
                    break
                if attempt < attempts:
                    time.sleep(retry_sleep)

            if _dd_strict_result_executed(last_result):
                break
            # timeout / no-order / hard-filter NG でも次の1銘柄候補へ進む。

        if isinstance(result, dict):
            out = dict(result)
            out["direct_dispatch_sync_fallback"] = True
            out["direct_dispatch_rolling"] = True
            out["direct_dispatch_timeout_continue"] = True
            out["direct_dispatch_one_by_one"] = True
            out["direct_dispatch_fresh_guard"] = True
            out["direct_dispatch_skip_original_failed_rows"] = bool(skip_original)
            out["direct_dispatch_timeout_seen"] = bool(timeout_seen)
            out["direct_dispatch_freshness_expired_seen"] = bool(freshness_expired_seen)
            out["direct_dispatch_attempts"] = attempt_records
            out["direct_dispatch_result"] = last_result
            out["summary_ai_ok_valid_sec"] = max_age_sec
            out["summary_ai_direct_snapshot_timeout_sec"] = timeout_sec
            if _dd_strict_result_executed(last_result):
                out["executed"] = True
                out["skip_reason"] = None
            elif freshness_expired_seen:
                out["executed"] = False
                out["skip_reason"] = "summary_ai_ok_expired_before_dispatch"
            elif timeout_seen:
                out["executed"] = False
                out["skip_reason"] = "direct_snapshot_timeout_after_candidates"
                out["direct_dispatch_timeout"] = True
            else:
                out["executed"] = False
                out["skip_reason"] = _dd_flatten_reasons(last_result) or "entry_pipeline_no_order"
            return out
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] sync fallback failed")
    return result


def _direct_dispatch_layer(ai_results: Any, *, df_summary: Any, interval: Any, max_entries: int, dry_run: bool, require_market_open: bool, entry_pipeline: Optional[Callable[..., Any]]) -> Dict[str, Any]:
    _dd_force_direct_sync_env()
    result = _async_entry_layer(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)
    if dry_run:
        return result
    return _dd_fallback_direct_dispatch(result, {"ai_results": ai_results, "interval": interval})


# ============================================================
# async-entry sync/queue dispatch (ported from
# core/startup/summary_ai_async_entry_patch.py V12)
# ============================================================

_ASYNC_LOCK = threading.Lock()
_ASYNC_QUEUE: "deque[Dict[str, Any]]" = deque()
_ASYNC_WORKER_RUNNING = False
_ASYNC_SEQ = 0


def _async_market_open_now() -> bool:
    try:
        return bool(is_market_open())
    except Exception:
        return True


def _async_execute_original(item: Dict[str, Any]) -> Any:
    return _rev10_execute_ai_ok_entries_bulk(
        item["ai_results"], df_summary=item["df_summary"], interval=item["interval"], max_entries=item["max_entries"],
        dry_run=False, require_market_open=item["require_market_open"], entry_pipeline=item["entry_pipeline"],
    )


def _async_unwrap_result(result: Any) -> Any:
    cur = result
    try:
        for _ in range(12):
            if isinstance(cur, dict) and isinstance(cur.get("result"), dict):
                cur = cur.get("result")
                continue
            if isinstance(cur, dict) and isinstance(cur.get("pipeline_result"), dict):
                cur = cur.get("pipeline_result")
                continue
            break
    except Exception:
        return result
    return cur


def _async_skip_reason(result: Any) -> str:
    reasons: List[str] = []
    try:
        cur = result
        for _ in range(16):
            if not isinstance(cur, dict):
                break
            for k in ("skip_reason", "lock_wait_reason", "reason", "status"):
                r = cur.get(k)
                if r:
                    reasons.append(str(r))
            nxt = cur.get("result") or cur.get("pipeline_result")
            if not isinstance(nxt, dict):
                break
            cur = nxt
    except Exception:
        pass
    return "|".join(reasons)


def _async_is_retryable_controller_busy(result: Any) -> bool:
    try:
        text = _async_skip_reason(result).lower()
        unwrapped = _async_unwrap_result(result)
        retryable = bool(unwrapped.get("retryable")) if isinstance(unwrapped, dict) else False
        retry_markers = (
            "entry_controller_lock_timeout", "lock_timeout", "pipeline_busy", "already_running",
            "queued_async", "pending_moved_without_order", "order_id_empty_retryable",
        )
        if retryable or any(x in text for x in retry_markers):
            return True
        hard_no_retry = ("snapshot_no_ai_approved_candidates", "no_tradable_rows_after_filters", "market_closed", "risk_guard_ng", "ai_health_ng", "index_shock")
        if any(x in text for x in hard_no_retry):
            return False
    except Exception:
        return False
    return False


def _async_summarize_result(result: Any) -> Dict[str, Any]:
    try:
        if isinstance(result, dict):
            return {
                "executed": bool(result.get("executed")), "submitted_async": bool(result.get("submitted_async")),
                "skip_reason": result.get("skip_reason"), "reason_chain": _async_skip_reason(result),
            }
        return {"result_type": type(result).__name__}
    except Exception as e:
        return {"summary_error": str(e)}


def _async_run_worker_loop() -> None:
    global _ASYNC_WORKER_RUNNING
    while True:
        with _ASYNC_LOCK:
            if not _ASYNC_QUEUE:
                _ASYNC_WORKER_RUNNING = False
                logger.info("[SUMMARY AI ASYNC ENTRY] queue worker idle")
                return
            item = _ASYNC_QUEUE.popleft()
        seq = item["seq"]
        started = time.time()
        queued_at = float(item.get("queued_at") or started)
        stale_sec = _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 20.0)
        try:
            if stale_sec > 0 and time.time() - queued_at > stale_sec:
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker skip stale seq=%s", seq)
                continue
            if bool(item.get("require_market_open")) and not _async_market_open_now():
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker skip market_closed seq=%s", seq)
                continue
            retry_enabled = _env_bool("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", True)
            retry_max = max(1, _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 3))
            retry_sleep = max(0.2, _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 1.0))
            result: Any = None
            for attempt in range(1, retry_max + 1):
                attempt_started = time.time()
                result = _async_execute_original(item)
                summary = _async_summarize_result(result)
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker attempt done seq=%s attempt=%s/%s elapsed=%.3fs summary=%s", seq, attempt, retry_max, time.time() - attempt_started, summary)
                if bool(summary.get("executed")):
                    break
                if not retry_enabled or not _async_is_retryable_controller_busy(result):
                    break
                if stale_sec > 0 and time.time() - queued_at + retry_sleep > stale_sec:
                    break
                if attempt < retry_max:
                    time.sleep(retry_sleep)
            logger.warning("[SUMMARY AI ASYNC ENTRY] worker done seq=%s elapsed=%.3fs final_summary=%s", seq, time.time() - started, _async_summarize_result(result))
        except Exception as e:
            logger.exception("[SUMMARY AI ASYNC ENTRY] worker failed seq=%s err=%s", seq, e)


def _async_ensure_worker_started() -> None:
    global _ASYNC_WORKER_RUNNING
    with _ASYNC_LOCK:
        if _ASYNC_WORKER_RUNNING:
            return
        _ASYNC_WORKER_RUNNING = True
    threading.Thread(target=_async_run_worker_loop, name="SummaryAiAsyncEntry", daemon=True).start()


def _async_entry_layer(ai_results: Any, *, df_summary: Any, interval: Any, max_entries: int, dry_run: bool, require_market_open: bool, entry_pipeline: Optional[Callable[..., Any]]) -> Dict[str, Any]:
    global _ASYNC_SEQ
    if dry_run or not _env_bool("SUMMARY_AI_ASYNC_ENTRY", True) or _env_bool("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", False):
        return _rev10_execute_ai_ok_entries_bulk(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)

    approved_rows = build_ai_ok_approved_rows(ai_results, max_entries=max_entries)
    if not approved_rows:
        return {"executed": False, "submitted_async": False, "dry_run": False, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok"}
    if require_market_open and not _async_market_open_now():
        return {"executed": False, "submitted_async": False, "dry_run": False, "approved_rows": approved_rows, "result": None, "skip_reason": "market_closed"}

    queue_max = max(1, min(_env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1), 3))
    with _ASYNC_LOCK:
        dropped = 0
        while len(_ASYNC_QUEUE) >= queue_max:
            _ASYNC_QUEUE.popleft()
            dropped += 1
        _ASYNC_SEQ += 1
        seq = _ASYNC_SEQ
        _ASYNC_QUEUE.append({
            "seq": seq, "queued_at": time.time(), "ai_results": ai_results, "df_summary": df_summary, "interval": interval,
            "max_entries": max_entries, "require_market_open": require_market_open, "entry_pipeline": entry_pipeline,
        })
        q_size = len(_ASYNC_QUEUE)
    _async_ensure_worker_started()
    logger.warning("[SUMMARY AI ASYNC ENTRY] queued seq=%s interval=%s approved=%s queue_size=%s dropped=%s", seq, interval, len(approved_rows), q_size, dropped)
    return {"executed": False, "submitted_async": True, "queued_async": True, "async_seq": seq, "queue_size": q_size, "dry_run": False, "approved_rows": approved_rows, "result": {"status": "queued_async", "seq": seq, "queue_size": q_size}, "skip_reason": "queued_async"}


def _lowmove_batch_size(default: int = 3) -> int:
    return max(1, min(_env_int("SUMMARY_AI_EXECUTOR_BATCH_SIZE", default), 3))


def _approved_batches(ai_results: Sequence[Dict[str, Any]], *, max_entries: int) -> Iterable[List[Dict[str, Any]]]:
    _ensure_core_filter(reason="approved_batches")
    ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
    pool = _selected_pool(ok_items, max_entries=max_entries)
    cap = _effective_max_entries(max_entries)
    max_rounds = max(1, _env_int("SUMMARY_AI_ROLLING_RETRY_ROUNDS", 3))
    used: Set[str] = set()
    round_no = 0
    while pool and round_no < max_rounds:
        batch_items: List[Dict[str, Any]] = []
        rest: List[Dict[str, Any]] = []
        for item in pool:
            sym = _pick_symbol(item)
            key = f"{sym}:{_pick_side(item)}"
            if key in used:
                continue
            if len(batch_items) < cap:
                batch_items.append(item)
                used.add(key)
            else:
                rest.append(item)
        pool = rest
        if not batch_items:
            break
        round_no += 1
        logger.warning("[SUMMARY AI EXECUTOR] rolling batch round=%s/%s cap=%s symbols=%s version=%s", round_no, max_rounds, cap, [_pick_symbol(x) for x in batch_items], VERSION)
        yield [build_approved_row(x) for x in batch_items]


def _rev10_execute_ai_ok_entries_bulk(ai_results: Sequence[Dict[str, Any]], *, df_summary: pd.DataFrame, interval: int | str = 1, max_entries: int = DEFAULT_MAX_ENTRIES, dry_run: bool = True, require_market_open: bool = True, entry_pipeline: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    _ensure_core_filter(reason="execute_start")
    if require_market_open and not is_market_open():
        approved_rows = build_ai_ok_approved_rows(ai_results, max_entries=max_entries)
        return {"executed": False, "dry_run": dry_run, "approved_rows": approved_rows, "result": None, "skip_reason": "market_closed"}
    if entry_pipeline is None:
        entry_pipeline = get_bulk_entry_pipeline()
    if entry_pipeline is None:
        approved_rows = build_ai_ok_approved_rows(ai_results, max_entries=max_entries)
        return {"executed": False, "dry_run": dry_run, "approved_rows": approved_rows, "result": None, "skip_reason": "entry_pipeline_not_found"}

    all_approved: List[Dict[str, Any]] = []
    last_result: Any = None
    last_removed = 0
    last_skip = "no_ai_ok"
    try:
        batches = list(_approved_batches(ai_results, max_entries=max_entries)) if _env_bool("SUMMARY_AI_ROLLING_RETRY_ON_FILTERED_NO_ORDER", True) else [build_ai_ok_approved_rows(ai_results, max_entries=max_entries)]
        if not batches or not batches[0]:
            return {"executed": False, "dry_run": dry_run, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok_after_strict_prefilter"}
        for idx, approved_rows in enumerate(batches, start=1):
            all_approved.extend(approved_rows)
            if dry_run:
                return {"executed": False, "dry_run": True, "approved_rows": approved_rows, "result": None, "skip_reason": "dry_run"}
            logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry start approved=%s interval=%s symbols=%s round=%s/%s version=%s", len(approved_rows), interval, [str(x.get("symbol")) for x in approved_rows], idx, len(batches), VERSION)
            result = entry_pipeline(approved_rows, df_summary, interval)
            executed = _positive_result(result)
            last_result = result
            if executed:
                logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s executed=True round=%s result=%s", len(approved_rows), idx, result)
                return {"executed": True, "dry_run": False, "approved_rows": approved_rows, "all_approved_rows": all_approved, "result": result, "skip_reason": None, "pending_removed": 0, "rolling_retry_round": idx}
            last_skip = "entry_pipeline_no_order"
            if _retryable_no_tradable(result) and idx < len(batches):
                _cleanup_pending_after_no_order(result, approved_rows, reason="entry_pipeline_no_order_retry")
                logger.warning("[SUMMARY AI EXECUTOR] no tradable rows -> retry next AI_OK batch round=%s/%s symbols=%s detail=%s version=%s", idx, len(batches), [str(x.get("symbol")) for x in approved_rows], _summarize_no_order_result(result), VERSION)
                continue
            last_removed = _cleanup_pending_after_no_order(result, approved_rows, reason=last_skip)
            logger.warning("[SUMMARY AI EXECUTOR] NO REAL ORDER DETAIL approved=%s interval=%s symbols=%s skip=%s pending_removed=%s detail=%s", len(approved_rows), interval, [str(x.get("symbol")) for x in approved_rows], last_skip, last_removed, _summarize_no_order_result(result))
            break
        # REV10: also clean all rows selected across the rolling run, because wrapper patches may return only the final attempted result.
        if all_approved:
            last_removed = max(last_removed, _cleanup_pending_after_no_order(last_result, all_approved, reason=f"{last_skip}_all_approved"))
        logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s executed=False pending_removed=%s result=%s", len(all_approved), last_removed, last_result)
        return {"executed": False, "dry_run": False, "approved_rows": all_approved, "result": last_result, "skip_reason": last_skip, "pending_removed": last_removed, "rolling_retry_attempted": len(batches)}
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] REAL bulk entry failed")
        _cleanup_pending_after_no_order(last_result, all_approved, reason="entry_exception")
        return {"executed": False, "dry_run": False, "approved_rows": all_approved, "result": last_result, "skip_reason": "entry_exception"}


def execute_ai_ok_entries_bulk(ai_results: Sequence[Dict[str, Any]], *, df_summary: pd.DataFrame, interval: int | str = 1, max_entries: int = DEFAULT_MAX_ENTRIES, dry_run: bool = True, require_market_open: bool = True, entry_pipeline: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Entry point. Layering (outer -> inner), matching the production install
    order that used to be assembled at runtime via 4 monkeypatches:

      1. blow-off top prefilter (always applied first -- this was the bug fix,
         see REV11 changelog above: it used to be bypassed)
      2. low_move_softpass rolling-retry (batches AI_OK candidates, retries
         next batch on no-order)
      3. [fallback only, when rolling-retry disabled or raises] direct_dispatch
         (forces sync dispatch + its own rolling snapshot retry)
      4. [fallback only] async_entry (sync passthrough, or queue+worker thread
         when SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC=0)
      5. [fallback only] the native REV10 single/rolling-batch executor
    """
    _ensure_core_filter(reason="execute_start")
    before_n = len(list(ai_results or []))
    ai_results, blowoff_skipped, blowoff_top_symbols, blowoff_meta = _blowoff_filter_ai_results(ai_results, df_summary)
    after_n = len(ai_results)

    if not _env_bool("SUMMARY_AI_EXECUTOR_ROLLING_RETRY", True):
        result = _direct_dispatch_layer(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)
        return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)

    try:
        ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
        kept = _filter_blocked_ai_ok_items(ok_items)
        if not kept:
            result = {"executed": False, "dry_run": dry_run, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok"}
            return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
        if require_market_open and not is_market_open():
            approved_preview = [build_approved_row(x) for x in sorted(kept, key=_sort_key, reverse=True)[:_lowmove_batch_size(max_entries)]]
            result = {"executed": False, "dry_run": dry_run, "approved_rows": approved_preview, "result": None, "skip_reason": "market_closed"}
            return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
        if dry_run:
            approved_preview = [build_approved_row(x) for x in sorted(kept, key=_sort_key, reverse=True)[:_lowmove_batch_size(max_entries)]]
            result = {"executed": False, "dry_run": True, "approved_rows": approved_preview, "result": None, "skip_reason": "dry_run"}
            return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
        if entry_pipeline is None:
            entry_pipeline = get_bulk_entry_pipeline()
        if entry_pipeline is None:
            result = {"executed": False, "dry_run": False, "approved_rows": [], "result": None, "skip_reason": "entry_pipeline_not_found"}
            return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)

        batch_n = _lowmove_batch_size(max_entries)
        scan_limit = max(batch_n, _env_int("SUMMARY_AI_EXECUTOR_CANDIDATE_SCAN_LIMIT", 12))
        ordered = sorted(kept, key=_sort_key, reverse=True)[:scan_limit]
        all_rows: List[Dict[str, Any]] = []
        attempts: List[Dict[str, Any]] = []
        logger.warning("[SUMMARY AI EXECUTOR ROLLING] start ok_total=%s kept=%s scan=%s batch=%s interval=%s version=%s", len(ok_items), len(kept), len(ordered), batch_n, interval, VERSION)

        for start in range(0, len(ordered), batch_n):
            batch_items = ordered[start:start + batch_n]
            approved_rows = [build_approved_row(x) for x in batch_items]
            all_rows.extend(approved_rows)
            symbols = [str(x.get("symbol")) for x in approved_rows]
            logger.warning("[SUMMARY AI EXECUTOR ROLLING] batch start offset=%s size=%s symbols=%s", start, len(approved_rows), symbols)
            result = entry_pipeline(approved_rows, df_summary, interval)
            executed = _positive_result(result)
            attempts.append({"offset": start, "symbols": symbols, "executed": executed, "result": _summarize_no_order_result(result)})
            if executed:
                logger.warning("[SUMMARY AI EXECUTOR ROLLING] executed offset=%s symbols=%s result=%s", start, symbols, result)
                result_out = {"executed": True, "dry_run": False, "approved_rows": all_rows, "result": result, "skip_reason": None, "attempts": attempts, "rolling_retry": True}
                return _attach_blowoff_diagnostics(result_out, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
            logger.warning("[SUMMARY AI EXECUTOR ROLLING] batch no-order offset=%s symbols=%s detail=%s", start, symbols, _summarize_no_order_result(result))

        removed_pending = 0
        if all_rows:
            try:
                removed_pending = _cleanup_pending_after_no_order(attempts[-1].get("result") if attempts else None, all_rows, reason="entry_pipeline_no_order_all_batches")
            except Exception:
                logger.exception("[SUMMARY AI EXECUTOR ROLLING] final pending cleanup failed")
        result = {"executed": False, "dry_run": False, "approved_rows": all_rows, "result": attempts[-1].get("result") if attempts else None, "skip_reason": "entry_pipeline_no_order_all_batches", "attempts": attempts, "pending_removed": removed_pending, "rolling_retry": True}
        return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR ROLLING] rolling retry failed; fallback to direct-dispatch chain version=%s", VERSION)
        result = _direct_dispatch_layer(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)
        return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)


_start_filter_watcher()

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MIN_BUY_APPROVED",
    "DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY",
    "DEFAULT_MIN_PRICE_FOR_ENTRY",
    "build_approved_row",
    "build_ai_ok_approved_rows",
    "execute_ai_ok_entries_bulk",
    "_select_ai_ok_items",
    "_filter_blocked_ai_ok_items",
    "_effective_max_entries",
    "_pick_symbol",
    "_pick_side",
    "_row_side",
    "_row_score_for_side",
    "_score_for_side",
    "_sort_key",
    "_price_bounds",
    "_entry_price_bounds",
    "_positive_result",
]
