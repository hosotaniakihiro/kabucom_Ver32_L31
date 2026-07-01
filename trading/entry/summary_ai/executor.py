# ============================================================
# File   : trading/entry/summary_ai/executor.py
# Version: REV2-TOP3-APPROVAL-CAP
# ------------------------------------------------------------
# AI_OK rows -> approved_rows -> entry_pipeline.
# 実発注対象は最大3件に制限し、板API/発注ラッパーの過負荷を防ぐ。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from .utils import get_bulk_entry_pipeline, is_market_open, safe_float

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 3
DEFAULT_MIN_BUY_APPROVED = 0
DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY = 7000.0
DEFAULT_MIN_PRICE_FOR_ENTRY = 3000.0


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _norm_side(v: Any, default: str = "BUY") -> str:
    s = str(v or default).strip().upper()
    return s if s in {"BUY", "SELL"} else default


def _as_dict(v: Any) -> Dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _pick_symbol(item: Dict[str, Any]) -> str:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return _norm_symbol(item.get("symbol") or ai_row.get("symbol") or src.get("symbol"))


def _pick_side(item: Dict[str, Any]) -> str:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return _norm_side(item.get("side") or item.get("ai_side") or ai_row.get("side") or ai_row.get("ai_side") or ai_row.get("entry_decision") or src.get("side") or src.get("ai_side") or src.get("entry_decision"), "BUY")


def _pick_price(item: Dict[str, Any]) -> float:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    for d in (item, ai_row, src):
        for k in ("close_price", "price", "current_price", "close", "last_price", "CurrentPrice"):
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    return 0.0


def _score_for_side(item: Dict[str, Any]) -> float:
    side = _pick_side(item)
    if side == "SELL":
        return max(safe_float(item.get("sell_score")), abs(safe_float(item.get("score_total"))), abs(safe_float(item.get("final_score"))))
    return max(safe_float(item.get("buy_score")), safe_float(item.get("score_total")), safe_float(item.get("final_score")))


def _sort_key(item: Dict[str, Any]) -> tuple[float, float, float]:
    side = _pick_side(item)
    return (safe_float(item.get("confidence")), _score_for_side(item), safe_float(item.get("sell_score")) if side == "SELL" else safe_float(item.get("buy_score")))


def _price_bounds() -> tuple[float, float, dict[str, Any]]:
    min_price = DEFAULT_MIN_PRICE_FOR_ENTRY
    max_price = DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY
    diag: dict[str, Any] = {"source": "fallback"}
    try:
        from trading.entry.entry_budget import get_entry_min_price, get_entry_max_price, get_effective_entry_max_price
        min_price = float(get_entry_min_price())
        max_price = float(get_effective_entry_max_price() or get_entry_max_price() or max_price)
        diag["source"] = "entry_budget"
    except Exception:
        pass
    min_price = _env_float("SUMMARY_AI_ENTRY_MIN_PRICE", _env_float("ENTRY_MIN_PRICE", min_price))
    max_price = _env_float("SUMMARY_AI_ENTRY_MAX_PRICE_FOR_100_SHARE", _env_float("ENTRY_MAX_PRICE", max_price))
    diag.update({"effective_min_price": min_price, "effective_max_price": max_price})
    return min_price, max_price, diag


def _is_trade_restricted(symbol: str) -> bool:
    try:
        from global_state import global_data
        root = getattr(global_data, "trade_restricted", {}) or {}
        return bool(root.get(symbol))
    except Exception:
        return False


def _is_sell_reject_cached(symbol: str, side: str) -> bool:
    if side != "SELL":
        return False
    try:
        from AI.sell_order_reject_cache import is_sell_rejected
        return bool(is_sell_rejected(symbol))
    except Exception:
        return False


def _effective_max_entries(max_entries: int) -> int:
    try:
        requested = int(max_entries or DEFAULT_MAX_ENTRIES)
    except Exception:
        requested = DEFAULT_MAX_ENTRIES
    hard_cap = _env_int("SUMMARY_AI_MAX_REAL_ENTRIES", DEFAULT_MAX_ENTRIES)
    return max(1, min(requested, hard_cap, 3))


def _select_ai_ok_items(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    if not ok_items:
        return []
    min_price, max_price, diag = _price_bounds()
    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in ok_items:
        symbol = _pick_symbol(item)
        side = _pick_side(item)
        price = _pick_price(item)
        if price > 0 and min_price > 0 and price < min_price:
            skipped.append({"symbol": symbol, "side": side, "reason": "price_below_min", "price": price})
            continue
        if price > 0 and max_price > 0 and price > max_price:
            skipped.append({"symbol": symbol, "side": side, "reason": "price_over_max", "price": price})
            continue
        if _is_trade_restricted(symbol):
            skipped.append({"symbol": symbol, "side": side, "reason": "trade_restricted"})
            continue
        if _is_sell_reject_cached(symbol, side):
            skipped.append({"symbol": symbol, "side": side, "reason": "sell_reject_cache"})
            continue
        kept.append(item)
    if skipped:
        logger.warning("[SUMMARY AI EXECUTOR] prefiltered before=%s after=%s price_diag=%s skipped=%s", len(ok_items), len(kept), diag, skipped[:50])
    max_n = _effective_max_entries(max_entries)
    selected = sorted(kept, key=_sort_key, reverse=True)[:max_n]
    logger.warning("[SUMMARY AI EXECUTOR] top3 selection requested=%s cap=%s ok_total=%s selected=%s", max_entries, max_n, len(kept), [{"symbol": _pick_symbol(x), "side": _pick_side(x), "price": _pick_price(x), "conf": round(safe_float(x.get("confidence")), 3), "score": round(_score_for_side(x), 3)} for x in selected])
    return selected


def _positive_result(result: Any) -> bool:
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for k in ("executed", "order_sent", "order_submitted", "success", "approved", "entry_executed"):
            if bool(result.get(k)):
                return True
        for k in ("order_id", "OrderId", "orders", "order_ids", "sent_orders"):
            v = result.get(k)
            if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                return True
            if not isinstance(v, (list, tuple, set, dict)) and v:
                return True
        return False
    if isinstance(result, (list, tuple, set)):
        return len(result) > 0
    return bool(result)


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


def execute_ai_ok_entries_bulk(ai_results: Sequence[Dict[str, Any]], *, df_summary: pd.DataFrame, interval: int | str = 1, max_entries: int = DEFAULT_MAX_ENTRIES, dry_run: bool = True, require_market_open: bool = True, entry_pipeline: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    approved_rows = build_ai_ok_approved_rows(ai_results, max_entries=max_entries)
    if not approved_rows:
        return {"executed": False, "dry_run": dry_run, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok"}
    if require_market_open and not is_market_open():
        return {"executed": False, "dry_run": dry_run, "approved_rows": approved_rows, "result": None, "skip_reason": "market_closed"}
    if dry_run:
        return {"executed": False, "dry_run": True, "approved_rows": approved_rows, "result": None, "skip_reason": "dry_run"}
    if entry_pipeline is None:
        entry_pipeline = get_bulk_entry_pipeline()
    if entry_pipeline is None:
        return {"executed": False, "dry_run": False, "approved_rows": approved_rows, "result": None, "skip_reason": "entry_pipeline_not_found"}
    try:
        logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry start approved=%s interval=%s symbols=%s", len(approved_rows), interval, [str(x.get("symbol")) for x in approved_rows])
        result = entry_pipeline(approved_rows, df_summary, interval)
        executed = _positive_result(result)
        logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s executed=%s result=%s", len(approved_rows), executed, result)
        return {"executed": executed, "dry_run": False, "approved_rows": approved_rows, "result": result, "skip_reason": None if executed else "entry_pipeline_no_order"}
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] REAL bulk entry failed")
        return {"executed": False, "dry_run": False, "approved_rows": approved_rows, "result": None, "skip_reason": "entry_exception"}
