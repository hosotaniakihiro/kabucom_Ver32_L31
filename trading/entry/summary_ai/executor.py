# ============================================================
# File   : trading/entry/summary_ai/executor.py
# Version: REV6-NO-ORDER-ROLLING-RETRY
# ------------------------------------------------------------
# AI_OK rows -> approved_rows -> entry_pipeline.
#
# REV6:
#   - approved候補が entry_pipeline 側の厳密ガード
#     blowoff / low-move / liquidity 等で全滅した場合、
#     ガードは緩めず、未試行のAI_OK次候補を少数だけ繰り上げて再投入する。
#   - 実発注成功判定はREV5同様、order_id / sent_orders / executed=True 等の
#     実注文証跡だけを成功扱いにする。
#
# REV5:
#   - entry_pipeline_no_order / snapshot_no_order / entry_controller_no_order 時は
#     SUMMARY_AI pending を安全に掃除し、次回エントリーを詰まらせない。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

import pandas as pd

from .utils import get_bulk_entry_pipeline, is_market_open, safe_float

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 3
DEFAULT_MIN_BUY_APPROVED = 0
DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY = 7000.0
DEFAULT_MIN_PRICE_FOR_ENTRY = 3000.0


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


def _pick_symbol(item: Dict[str, Any]) -> str:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return _norm_symbol(item.get("symbol") or ai_row.get("symbol") or src.get("symbol"))


def _pick_side(item: Dict[str, Any]) -> str:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return _norm_side(
        item.get("side")
        or item.get("ai_side")
        or ai_row.get("side")
        or ai_row.get("ai_side")
        or ai_row.get("entry_decision")
        or src.get("side")
        or src.get("ai_side")
        or src.get("entry_decision"),
        "BUY",
    )


def _row_side(item: Dict[str, Any]) -> str:
    return _pick_side(item)


def _pick_price(item: Dict[str, Any]) -> float:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    for d in (item, ai_row, src):
        for k in ("close_price", "price", "current_price", "close", "last_price", "CurrentPrice"):
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    return 0.0


def _pick_volume(item: Dict[str, Any]) -> float:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    for d in (item, ai_row, src):
        for k in ("volume", "Volume", "trading_volume", "TradingVolume", "出来高"):
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    return 0.0


def _pick_turnover(item: Dict[str, Any]) -> float:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    for d in (item, ai_row, src):
        for k in ("turnover", "trading_value", "TradingValue", "売買代金"):
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    price = _pick_price(item)
    vol = _pick_volume(item)
    return price * vol if price > 0 and vol > 0 else 0.0


def _score_for_side(item: Dict[str, Any]) -> float:
    side = _pick_side(item)
    if side == "SELL":
        return max(safe_float(item.get("sell_score")), abs(safe_float(item.get("score_total"))), abs(safe_float(item.get("final_score"))))
    return max(safe_float(item.get("buy_score")), safe_float(item.get("score_total")), safe_float(item.get("final_score")))


def _row_score_for_side(item: Dict[str, Any]) -> float:
    return _score_for_side(item)


def _sort_key(item: Dict[str, Any]) -> tuple[float, float, float]:
    side = _pick_side(item)
    return (
        safe_float(item.get("confidence")),
        _score_for_side(item),
        safe_float(item.get("sell_score")) if side == "SELL" else safe_float(item.get("buy_score")),
    )


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
                if not isinstance(detail, dict):
                    detail = {"detail": str(detail)}
                return True, str(reason or "DAILY_RISK_BLOCK"), dict(detail)
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] daily risk compatibility check failed; fail-open", exc_info=True)
    return False, "", {}


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
        daily_blocked, daily_reason, daily_detail = _daily_risk_block_reason(symbol, side)
        if daily_blocked:
            skipped.append({"symbol": symbol, "side": side, "reason": daily_reason, "detail": daily_detail})
            continue
        restricted, until = _is_trade_restricted_symbol(symbol)
        if restricted:
            skipped.append({"symbol": symbol, "side": side, "reason": "trade_restricted", "until": str(until)})
            continue
        sell_rejected, reject_reason = _is_sell_reject_cached(symbol, side)
        if sell_rejected:
            skipped.append({"symbol": symbol, "side": side, "reason": "sell_reject_cache", "detail": str(reject_reason)})
            continue
        kept.append(item)
    if skipped:
        logger.warning("[SUMMARY AI EXECUTOR] prefiltered before=%s after=%s price_diag=%s skipped=%s", len(ok_items), len(kept), diag, skipped[:50])
    return kept


def _filter_blocked_ai_ok_items(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _base_filter_blocked_ai_ok_items(ok_items)


def _effective_max_entries(max_entries: int) -> int:
    try:
        requested = int(max_entries or DEFAULT_MAX_ENTRIES)
    except Exception:
        requested = DEFAULT_MAX_ENTRIES
    hard_cap = _env_int("SUMMARY_AI_MAX_REAL_ENTRIES", DEFAULT_MAX_ENTRIES)
    return max(1, min(requested, hard_cap, 3))


def _selected_pool(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    kept = _filter_blocked_ai_ok_items(ok_items)
    if not kept:
        return []
    hard_cap = _effective_max_entries(max_entries)
    pool_n = max(hard_cap, _env_int("SUMMARY_AI_ROLLING_RETRY_POOL", _env_int("SUMMARY_AI_EXECUTOR_SELECTION_POOL", 20)))
    pool_n = min(max(1, pool_n), len(kept))
    return sorted(kept, key=_sort_key, reverse=True)[:pool_n]


def _select_ai_ok_items(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    pool = _selected_pool(ok_items, max_entries=max_entries)
    selected = pool[:_effective_max_entries(max_entries)]
    logger.warning(
        "[SUMMARY AI EXECUTOR] top selection requested=%s cap=%s pool=%s ok_total=%s selected=%s version=%s",
        max_entries,
        _effective_max_entries(max_entries),
        len(pool),
        len(ok_items or []),
        [{"symbol": _pick_symbol(x), "side": _pick_side(x), "price": _pick_price(x), "conf": round(safe_float(x.get("confidence")), 3), "score": round(_score_for_side(x), 3)} for x in selected],
        "REV6",
    )
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
    if not symbols:
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
        logger.warning("[SUMMARY AI EXECUTOR] pending cleanup after no-order reason=%s symbols=%s removed=%s root=%s result=%s", reason, sorted(symbols), removed, snapshot_root(), _summarize_no_order_result(result))
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
    logger.info("[SUMMARY AI EXECUTOR] approved row built symbol=%s side=%s conf=%.3f total=%.3f close=%s", row.get("symbol"), side, safe_float(row.get("ai_confidence")), safe_float(row.get("score_total")), row.get("close_price"))
    return row


def build_ai_ok_approved_rows(ai_results: Sequence[Dict[str, Any]], *, max_entries: int = DEFAULT_MAX_ENTRIES) -> List[Dict[str, Any]]:
    ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
    approved = [build_approved_row(x) for x in _select_ai_ok_items(ok_items, max_entries=max_entries)]
    logger.info("[SUMMARY AI EXECUTOR] approved selection max_entries=%s rows=%s", max_entries, len(approved))
    return approved


def _approved_batches(ai_results: Sequence[Dict[str, Any]], *, max_entries: int) -> Iterable[List[Dict[str, Any]]]:
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
        logger.warning("[SUMMARY AI EXECUTOR] rolling batch round=%s/%s cap=%s symbols=%s", round_no, max_rounds, cap, [_pick_symbol(x) for x in batch_items])
        yield [build_approved_row(x) for x in batch_items]


def execute_ai_ok_entries_bulk(
    ai_results: Sequence[Dict[str, Any]],
    *,
    df_summary: pd.DataFrame,
    interval: int | str = 1,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    dry_run: bool = True,
    require_market_open: bool = True,
    entry_pipeline: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
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
            return {"executed": False, "dry_run": dry_run, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok"}

        for idx, approved_rows in enumerate(batches, start=1):
            all_approved.extend(approved_rows)
            if dry_run:
                return {"executed": False, "dry_run": True, "approved_rows": approved_rows, "result": None, "skip_reason": "dry_run"}
            logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry start approved=%s interval=%s symbols=%s round=%s/%s", len(approved_rows), interval, [str(x.get("symbol")) for x in approved_rows], idx, len(batches))
            result = entry_pipeline(approved_rows, df_summary, interval)
            executed = _positive_result(result)
            last_result = result
            if executed:
                logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s executed=True round=%s result=%s", len(approved_rows), idx, result)
                return {"executed": True, "dry_run": False, "approved_rows": approved_rows, "all_approved_rows": all_approved, "result": result, "skip_reason": None, "pending_removed": 0, "rolling_retry_round": idx}

            last_skip = "entry_pipeline_no_order"
            if _retryable_no_tradable(result) and idx < len(batches):
                _cleanup_pending_after_no_order(result, approved_rows, reason="entry_pipeline_no_order_retry")
                logger.warning("[SUMMARY AI EXECUTOR] no tradable rows -> retry next AI_OK batch round=%s/%s symbols=%s detail=%s", idx, len(batches), [str(x.get("symbol")) for x in approved_rows], _summarize_no_order_result(result))
                continue
            last_removed = _cleanup_pending_after_no_order(result, approved_rows, reason=last_skip)
            logger.warning("[SUMMARY AI EXECUTOR] NO REAL ORDER DETAIL approved=%s interval=%s symbols=%s skip=%s pending_removed=%s detail=%s", len(approved_rows), interval, [str(x.get("symbol")) for x in approved_rows], last_skip, last_removed, _summarize_no_order_result(result))
            break

        logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s executed=False pending_removed=%s result=%s", len(all_approved), last_removed, last_result)
        return {"executed": False, "dry_run": False, "approved_rows": all_approved, "result": last_result, "skip_reason": last_skip, "pending_removed": last_removed, "rolling_retry_attempted": len(batches)}
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] REAL bulk entry failed")
        _cleanup_pending_after_no_order(last_result, all_approved, reason="entry_exception")
        return {"executed": False, "dry_run": False, "approved_rows": all_approved, "result": last_result, "skip_reason": "entry_exception"}


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
