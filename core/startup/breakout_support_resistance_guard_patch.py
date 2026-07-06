# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

logger = logging.getLogger(__name__)
VERSION = "V1-BREAKOUT-SUPPORT-RESISTANCE-GUARD"
_INSTALLED = False
_ORIG_BASE_FILTER = None
_ORIG_SORT_KEY = None
_ORIG_BUILD_ORDER = None


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
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return "BUY"


def _as_dict(v: Any) -> dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _iter_sources(item: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield item
    for k in ("entry_row", "source_row", "ai_row", "row", "conditions", "entry_conditions"):
        d = item.get(k)
        if isinstance(d, dict):
            yield d


def _first(item: dict[str, Any], names: tuple[str, ...]) -> float:
    for d in _iter_sources(item):
        for n in names:
            x = _safe_float(d.get(n), 0.0)
            if x > 0:
                return x
    return 0.0


def _pick_symbol(item: dict[str, Any]) -> str:
    for d in _iter_sources(item):
        v = d.get("symbol") or d.get("Symbol") or d.get("code") or d.get("銘柄コード")
        if v:
            s = str(v).strip()
            if s.endswith(".0") and s[:-2].isdigit():
                s = s[:-2]
            return s
    return ""


def _pick_side(item: dict[str, Any]) -> str:
    for d in _iter_sources(item):
        v = d.get("side") or d.get("entry_decision") or d.get("ai_side")
        if v:
            return _norm_side(v)
    return "BUY"


def _pick_price(item: dict[str, Any]) -> float:
    return _first(item, ("price", "close_price", "current_price", "close", "last_price", "display_price", "CurrentPrice"))


def _pick_volume(item: dict[str, Any]) -> float:
    return _first(item, ("volume", "vol", "latest_volume", "display_volume", "Volume", "TradingVolume"))


def _pick_recent_avg_volume(item: dict[str, Any]) -> float:
    return _first(item, ("avg_volume_5", "volume_ma5", "avg_5m_volume", "recent_avg_volume", "volume_avg", "rolling_volume_avg"))


def _pick_level(item: dict[str, Any], names: tuple[str, ...]) -> float:
    return _first(item, names)


def _level_names() -> dict[str, tuple[str, ...]]:
    return {
        "prev_high": ("prev_high", "previous_high", "prev_day_high", "previous_day_high", "yesterday_high", "prior_high", "daily_prev_high", "前日高値"),
        "prev_low": ("prev_low", "previous_low", "prev_day_low", "previous_day_low", "yesterday_low", "prior_low", "daily_prev_low", "前日安値"),
        "resistance": ("resistance", "resistance_price", "upper_resistance", "recent_high", "recent_swing_high", "breakout_high", "rolling_high", "high_20", "high_10", "day_high", "today_high", "session_high", "intraday_high"),
        "support": ("support", "support_price", "lower_support", "recent_low", "recent_swing_low", "breakdown_low", "rolling_low", "low_20", "low_10", "day_low", "today_low", "session_low", "intraday_low"),
    }


def _score(item: dict[str, Any]) -> float:
    side = _pick_side(item)
    keys = ("sell_score", "score_total", "final_score", "score") if side == "SELL" else ("buy_score", "score_total", "final_score", "score")
    return max(_safe_float(item.get(k), 0.0) for k in keys)


def _diagnose(item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    side = _pick_side(item)
    symbol = _pick_symbol(item)
    price = _pick_price(item)
    if price <= 0:
        return True, {"reason": "no_price", "symbol": symbol, "side": side}
    names = _level_names()
    prev_high = _pick_level(item, names["prev_high"])
    prev_low = _pick_level(item, names["prev_low"])
    resistance = max(prev_high, _pick_level(item, names["resistance"]))
    support_candidates = [x for x in (prev_low, _pick_level(item, names["support"])) if x > 0]
    support = min(support_candidates) if support_candidates else 0.0
    near_pct = _env_float("BREAKOUT_SR_NEAR_BLOCK_PCT", 0.0015)
    break_pct = _env_float("BREAKOUT_SR_BREAK_CONFIRM_PCT", 0.0005)
    strong_break_pct = _env_float("BREAKOUT_SR_STRONG_BREAK_PCT", 0.0020)
    require_volume = _env_bool("BREAKOUT_SR_REQUIRE_VOLUME_FOR_BREAK", False)
    vol = _pick_volume(item)
    avg_vol = _pick_recent_avg_volume(item)
    volume_ok = True
    if require_volume and avg_vol > 0 and vol > 0:
        volume_ok = vol >= avg_vol * _env_float("BREAKOUT_SR_VOLUME_RATIO", 1.15)
    detail = {
        "symbol": symbol,
        "side": side,
        "price": price,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "resistance": resistance,
        "support": support,
        "near_pct": near_pct,
        "break_pct": break_pct,
        "strong_break_pct": strong_break_pct,
        "volume": vol,
        "avg_volume": avg_vol,
        "volume_ok": volume_ok,
        "score": _score(item),
    }
    if side == "BUY":
        if resistance > 0:
            gap = (resistance - price) / price
            break_margin = (price - resistance) / resistance if resistance > 0 else 0.0
            detail.update({"gap_to_resistance_pct": gap, "break_margin_pct": break_margin})
            if 0 <= gap <= near_pct:
                detail["reason"] = "near_resistance_block"
                return False, detail
            if break_margin >= break_pct and volume_ok:
                detail["reason"] = "resistance_breakout_ok"
                return True, detail
            if break_margin >= strong_break_pct:
                detail["reason"] = "strong_resistance_breakout_ok"
                return True, detail
        detail["reason"] = "no_resistance_level_or_not_near"
        return True, detail
    # SELL
    if support > 0:
        gap = (price - support) / price
        break_margin = (support - price) / support if support > 0 else 0.0
        detail.update({"gap_to_support_pct": gap, "break_margin_pct": break_margin})
        if 0 <= gap <= near_pct:
            detail["reason"] = "near_support_block"
            return False, detail
        if break_margin >= break_pct and volume_ok:
            detail["reason"] = "support_breakdown_ok"
            return True, detail
        if break_margin >= strong_break_pct:
            detail["reason"] = "strong_support_breakdown_ok"
            return True, detail
    detail["reason"] = "no_support_level_or_not_near"
    return True, detail


def _breakout_bonus(item: dict[str, Any]) -> float:
    if not isinstance(item, dict):
        return 0.0
    ok, detail = _diagnose(item)
    reason = str(detail.get("reason") or "")
    if not ok:
        return -1.0 * _env_float("BREAKOUT_SR_BLOCK_SORT_PENALTY", 1000.0)
    if "strong_" in reason:
        return _env_float("BREAKOUT_SR_STRONG_BONUS", 0.30)
    if "breakout_ok" in reason or "breakdown_ok" in reason:
        return _env_float("BREAKOUT_SR_BREAK_BONUS", 0.15)
    return 0.0


def _patch_summary_ai_executor() -> bool:
    global _ORIG_BASE_FILTER, _ORIG_SORT_KEY
    try:
        import trading.entry.summary_ai.executor as ex
        base = getattr(ex, "_base_filter_blocked_ai_ok_items", None)
        if callable(base) and not getattr(base, "_breakout_sr_guard_v1", False):
            _ORIG_BASE_FILTER = base
            def _patched_base_filter(ok_items):
                rows = _ORIG_BASE_FILTER(ok_items)
                if not _env_bool("BREAKOUT_SR_GUARD_ENABLED", True):
                    return rows
                kept = []
                skipped = []
                for item in rows or []:
                    if not isinstance(item, dict):
                        kept.append(item)
                        continue
                    ok, detail = _diagnose(item)
                    item["breakout_sr_detail"] = detail
                    item["breakout_sr_ok"] = bool(ok)
                    item["breakout_sr_reason"] = detail.get("reason")
                    if not ok:
                        skipped.append(detail)
                        continue
                    kept.append(item)
                if skipped:
                    logger.warning("[BREAKOUT SR GUARD] SUMMARY_AI skipped=%s kept=%s sample=%s version=%s", len(skipped), len(kept), skipped[:10], VERSION)
                return kept
            _patched_base_filter._breakout_sr_guard_v1 = True  # type: ignore[attr-defined]
            _patched_base_filter._original = base  # type: ignore[attr-defined]
            ex._base_filter_blocked_ai_ok_items = _patched_base_filter
            try:
                ex._ORIGINAL_FILTER_BLOCKED_AI_OK_ITEMS = _patched_base_filter
                ex._filter_blocked_ai_ok_items = _patched_base_filter
            except Exception:
                pass
        sort_key = getattr(ex, "_sort_key", None)
        if callable(sort_key) and not getattr(sort_key, "_breakout_sr_sort_v1", False):
            _ORIG_SORT_KEY = sort_key
            def _patched_sort_key(item):
                base_key = _ORIG_SORT_KEY(item)
                try:
                    if isinstance(base_key, tuple):
                        return tuple(float(x) if isinstance(x, (int, float)) else x for x in base_key) + (_breakout_bonus(item),)
                except Exception:
                    pass
                return base_key
            _patched_sort_key._breakout_sr_sort_v1 = True  # type: ignore[attr-defined]
            _patched_sort_key._original = sort_key  # type: ignore[attr-defined]
            ex._sort_key = _patched_sort_key
        logger.warning("[BREAKOUT SR GUARD] summary_ai executor patched version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[BREAKOUT SR GUARD] summary_ai executor patch failed version=%s", VERSION)
        return False


def _patch_entry_order_builder() -> bool:
    global _ORIG_BUILD_ORDER
    try:
        from trading.handlers import entry_order_builder as eob
        cur = getattr(eob, "build_entry_order", None)
        if not callable(cur) or getattr(cur, "_breakout_sr_guard_v1", False):
            return True
        _ORIG_BUILD_ORDER = cur
        def _patched_build_entry_order(*args, **kwargs):
            try:
                row = kwargs.get("entry_row") if isinstance(kwargs.get("entry_row"), dict) else {}
                item = dict(row)
                item.setdefault("symbol", kwargs.get("symbol"))
                item.setdefault("side", kwargs.get("side"))
                if _env_bool("BREAKOUT_SR_ORDER_BUILDER_GUARD", True) and item:
                    ok, detail = _diagnose(item)
                    if not ok:
                        logger.warning("[BREAKOUT SR GUARD] ORDER_BUILD_NG detail=%s version=%s", detail, VERSION)
                        return {"ok": False, "reason": str(detail.get("reason") or "BREAKOUT_SR_GUARD_NG").upper(), "detail": detail}
                    row["breakout_sr_detail"] = detail
                    row["breakout_sr_reason"] = detail.get("reason")
            except Exception:
                logger.debug("[BREAKOUT SR GUARD] order builder precheck failed; fail-open", exc_info=True)
            return _ORIG_BUILD_ORDER(*args, **kwargs)
        _patched_build_entry_order._breakout_sr_guard_v1 = True  # type: ignore[attr-defined]
        _patched_build_entry_order._original = cur  # type: ignore[attr-defined]
        eob.build_entry_order = _patched_build_entry_order
        try:
            import trading.handlers.entry_controller as ec
            ec.build_entry_order = _patched_build_entry_order
        except Exception:
            pass
        logger.warning("[BREAKOUT SR GUARD] entry_order_builder patched version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[BREAKOUT SR GUARD] entry_order_builder patch failed version=%s", VERSION)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    ok1 = _patch_summary_ai_executor()
    ok2 = _patch_entry_order_builder()
    _INSTALLED = bool(ok1 or ok2)
    logger.warning(
        "[BREAKOUT SR GUARD] installed ok=%s summary_ai=%s order_builder=%s near=%.4f break=%.4f strong=%.4f volume_req=%s version=%s",
        _INSTALLED,
        ok1,
        ok2,
        _env_float("BREAKOUT_SR_NEAR_BLOCK_PCT", 0.0015),
        _env_float("BREAKOUT_SR_BREAK_CONFIRM_PCT", 0.0005),
        _env_float("BREAKOUT_SR_STRONG_BREAK_PCT", 0.0020),
        _env_bool("BREAKOUT_SR_REQUIRE_VOLUME_FOR_BREAK", False),
        VERSION,
    )
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[BREAKOUT SR GUARD] auto install failed")


__all__ = ["install", "VERSION"]
