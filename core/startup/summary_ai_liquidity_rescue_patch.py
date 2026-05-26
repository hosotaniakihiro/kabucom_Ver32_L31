# ============================================================
# File   : core/startup/summary_ai_liquidity_rescue_patch.py
# Version: V1.0-SUMMARY-AI-LIQUIDITY-ROW-VOLUME-RESCUE
# ------------------------------------------------------------
# 【目的】
#   SUMMARY AI で AI_OK が出ているのに、executor 直前の流動性チェックで
#   全候補が SUMMARY_AI_LIQ_LATEST_VOLUME_LOW となり approved=0 になる問題を緩和する。
#
# 【背景】
#   ログ上、candidate row には row_volume=258100 / 780700 など十分な出来高が
#   入っている一方、流動性チェックが古い/薄い直近1分足 latest_volume=200〜10600 を見て
#   全候補を落としているケースがある。
#
# 【方針】
#   - trading.entry.summary_ai.executor._filter_blocked_ai_ok_items をラップ
#   - 元フィルタが全落ちした場合のみ rescue を試す
#   - 価格レンジ、daily risk、trade_restricted、SELL reject cache は元 executor の関数で再確認
#   - row/source/ai_row の volume/trading_volume/row_volume が十分なら候補を残す
#   - 救済上限は SUMMARY_AI_LIQ_RESCUE_MAX_ITEMS で制御
#
# 【ENV】
#   SUMMARY_AI_LIQ_RESCUE_ENABLED=1
#   SUMMARY_AI_LIQ_RESCUE_MIN_ROW_VOLUME=30000
#   SUMMARY_AI_LIQ_RESCUE_MAX_ITEMS=10
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_FILTER = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s.replace(",", ""))
    except Exception:
        return default


def _as_dict(v: Any) -> Dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _pick_symbol(item: Dict[str, Any]) -> str:
    try:
        ai_row = _as_dict(item.get("ai_row"))
        src = _as_dict(item.get("source_row"))
        s = str(item.get("symbol") or ai_row.get("symbol") or src.get("symbol") or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _pick_side(executor, item: Dict[str, Any]) -> str:
    try:
        fn = getattr(executor, "_row_side", None)
        if callable(fn):
            return str(fn(item) or "BUY").upper()
    except Exception:
        pass
    try:
        return str(item.get("side") or item.get("ai_side") or "BUY").upper()
    except Exception:
        return "BUY"


def _pick_price(executor, item: Dict[str, Any]) -> float:
    try:
        fn = getattr(executor, "_pick_price", None)
        if callable(fn):
            return _safe_float(fn(item), 0.0)
    except Exception:
        pass
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    for d in (item, ai_row, src):
        for k in ("close_price", "price", "current_price", "close", "last_price", "CurrentPrice"):
            p = _safe_float(d.get(k), 0.0)
            if p > 0:
                return p
    return 0.0


def _pick_row_volume(item: Dict[str, Any]) -> float:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    vals = []
    for d in (item, ai_row, src):
        for k in (
            "row_volume",
            "volume",
            "trading_volume",
            "TradingVolume",
            "turnover_volume",
            "latest_row_volume",
        ):
            vals.append(_safe_float(d.get(k), 0.0))
    return max(vals) if vals else 0.0


def _passes_non_liquidity_executor_guards(executor, item: Dict[str, Any]) -> tuple[bool, str]:
    symbol = _pick_symbol(item)
    side = _pick_side(executor, item)
    price = _pick_price(executor, item)

    try:
        bounds_fn = getattr(executor, "_entry_price_bounds", None)
        if callable(bounds_fn):
            min_price, max_price, _diag = bounds_fn()
            if price > 0 and min_price > 0 and price < min_price:
                return False, f"price_below_entry_min_price:{price}<{min_price}"
            if price > 0 and max_price > 0 and price > max_price:
                return False, f"price_over_entry_max_price:{price}>{max_price}"
    except Exception:
        logger.debug("[SUMMARY AI LIQ RESCUE] price guard failed-open symbol=%s", symbol, exc_info=True)

    try:
        risk_fn = getattr(executor, "_daily_risk_block_reason", None)
        if callable(risk_fn):
            blocked, reason, detail = risk_fn(symbol, side)
            if blocked:
                return False, f"daily_risk:{reason}:{detail}"
    except Exception:
        logger.debug("[SUMMARY AI LIQ RESCUE] daily risk guard failed-open symbol=%s", symbol, exc_info=True)

    try:
        tr_fn = getattr(executor, "_is_trade_restricted_symbol", None)
        if callable(tr_fn):
            blocked, until = tr_fn(symbol)
            if blocked:
                return False, f"trade_restricted:{until}"
    except Exception:
        logger.debug("[SUMMARY AI LIQ RESCUE] trade restricted guard failed-open symbol=%s", symbol, exc_info=True)

    try:
        sell_fn = getattr(executor, "_is_sell_reject_cached", None)
        if callable(sell_fn):
            blocked, reason = sell_fn(symbol, side)
            if blocked:
                return False, f"sell_reject_cache:{reason}"
    except Exception:
        logger.debug("[SUMMARY AI LIQ RESCUE] sell reject guard failed-open symbol=%s", symbol, exc_info=True)

    return True, "ok"


def _patched_filter_blocked_ai_ok_items(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    import trading.entry.summary_ai.executor as executor

    original = _ORIGINAL_FILTER
    base = original(ok_items) if callable(original) else list(ok_items or [])

    if base:
        return base

    if not _env_bool("SUMMARY_AI_LIQ_RESCUE_ENABLED", True):
        return base

    if not ok_items:
        return base

    min_volume = _env_float("SUMMARY_AI_LIQ_RESCUE_MIN_ROW_VOLUME", 30000.0)
    max_items = max(1, _env_int("SUMMARY_AI_LIQ_RESCUE_MAX_ITEMS", 10))

    rescued: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for item in ok_items:
        if not isinstance(item, dict) or not bool(item.get("allow")):
            continue
        symbol = _pick_symbol(item)
        side = _pick_side(executor, item)
        price = _pick_price(executor, item)
        row_volume = _pick_row_volume(item)

        if row_volume < min_volume:
            skipped.append({"symbol": symbol, "side": side, "price": price, "row_volume": row_volume, "reason": "row_volume_low"})
            continue

        ok, reason = _passes_non_liquidity_executor_guards(executor, item)
        if not ok:
            skipped.append({"symbol": symbol, "side": side, "price": price, "row_volume": row_volume, "reason": reason})
            continue

        rescued.append(item)
        if len(rescued) >= max_items:
            break

    if rescued:
        logger.warning(
            "[SUMMARY AI LIQ RESCUE] all original AI_OK filtered; rescued by row_volume count=%s min_volume=%s symbols=%s skipped_head=%s",
            len(rescued),
            min_volume,
            [{"symbol": _pick_symbol(x), "side": _pick_side(executor, x), "price": _pick_price(executor, x), "row_volume": _pick_row_volume(x)} for x in rescued],
            skipped[:30],
        )
        return rescued

    logger.warning(
        "[SUMMARY AI LIQ RESCUE] no rescue after original all-filtered min_volume=%s skipped_head=%s",
        min_volume,
        skipped[:30],
    )
    return base


def install() -> bool:
    global _PATCHED, _ORIGINAL_FILTER
    if _PATCHED:
        return True
    try:
        import trading.entry.summary_ai.executor as executor

        cur = getattr(executor, "_filter_blocked_ai_ok_items", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI LIQ RESCUE] target filter not callable")
            return False
        if getattr(cur, "_summary_ai_liq_rescue_patch", False):
            _PATCHED = True
            return True

        _ORIGINAL_FILTER = cur
        _patched_filter_blocked_ai_ok_items._summary_ai_liq_rescue_patch = True  # type: ignore[attr-defined]
        executor._filter_blocked_ai_ok_items = _patched_filter_blocked_ai_ok_items

        _PATCHED = True
        logger.warning("[SUMMARY AI LIQ RESCUE] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI LIQ RESCUE] install failed")
        return False


__all__ = ["install"]
