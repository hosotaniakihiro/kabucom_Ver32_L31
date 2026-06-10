# ============================================================
# File   : core/startup/summary_ai_liquidity_rescue_patch.py
# Version: V1.1-SUMMARY-AI-LIQUIDITY-AND-ZERO-SCORE-CANDIDATE-RESCUE
# ------------------------------------------------------------
# 【目的】
#   1) SUMMARY AI で AI_OK が出ているのに、executor 直前の流動性チェックで
#      全候補が SUMMARY_AI_LIQ_LATEST_VOLUME_LOW となり approved=0 になる問題を緩和する。
#   2) PUSH/Yahoo recovery 行で score_buy/score_sell/slope が 0 固定となり、
#      AI 候補抽出前に BUY_SELL combined empty になる問題を緩和する。
#
# 【方針】
#   - executor._filter_blocked_ai_ok_items をラップし、全落ち時のみ row_volume で救済。
#   - trading.entry.summary_ai.candidates.attach_display_like_columns をラップし、
#     buy/sell score が全ゼロ、または slope が全ゼロの時だけ、OHLC / MACD / MTF / volume から
#     候補抽出用の ai_disp_* を fail-open 補完する。
#   - 価格/出来高/最終発注ガードは既存後段に残す。
#
# 【ENV】
#   SUMMARY_AI_LIQ_RESCUE_ENABLED=1
#   SUMMARY_AI_LIQ_RESCUE_MIN_ROW_VOLUME=30000
#   SUMMARY_AI_LIQ_RESCUE_MAX_ITEMS=10
#   SUMMARY_AI_ZERO_SCORE_RESCUE_ENABLED=1
#   SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_VOLUME=3000
#   SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_TURNOVER=1000000
#   SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_SCORE=4.1
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_FILTER = None
_ORIGINAL_ATTACH_DISPLAY = None


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


# ============================================================
# SUMMARY AI candidate zero-score rescue
# ============================================================

def _patched_attach_display_like_columns(df):
    original = _ORIGINAL_ATTACH_DISPLAY
    out = original(df) if callable(original) else df

    if not _env_bool("SUMMARY_AI_ZERO_SCORE_RESCUE_ENABLED", True):
        return out

    try:
        import pandas as pd
        import numpy as np

        if out is None or not isinstance(out, pd.DataFrame) or out.empty:
            return out

        work = out.copy()

        def num(col: str, default: float = 0.0):
            if col in work.columns:
                return pd.to_numeric(work[col], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(default)
            return pd.Series(default, index=work.index, dtype="float64")

        buy = num("ai_disp_buy_score")
        sell = num("ai_disp_sell_score").abs()
        slope = num("ai_disp_slope")
        close = num("ai_disp_close")
        volume = num("ai_disp_volume")
        turnover = num("ai_disp_turnover")
        open_px = num("open", np.nan)
        if open_px.isna().all():
            open_px = num("open_price", np.nan)
        high = num("high", np.nan)
        if high.isna().all():
            high = num("high_price", np.nan)
        low = num("low", np.nan)
        if low.isna().all():
            low = num("low_price", np.nan)
        mtf = num("ai_disp_mtf")
        macd = num("ai_disp_macd")
        sig = num("ai_disp_signal")

        min_vol = _env_float("SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_VOLUME", 3000.0)
        min_turnover = _env_float("SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_TURNOVER", 1000000.0)
        min_score = _env_float("SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_SCORE", 4.1)

        liquid = (volume >= min_vol) | (turnover >= min_turnover)
        price_ok = close > 0
        delta_pct = ((close - open_px) / open_px.replace(0, np.nan) * 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        range_pct = ((high - low) / close.replace(0, np.nan) * 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        macd_bias = (macd - sig).fillna(0.0)

        # score が全ゼロなら、MTF + 現在足方向 + MACD方向 + レンジを候補抽出用スコアへ変換する。
        score_all_zero = float(buy.abs().sum() + sell.abs().sum()) <= 0.000001
        if score_all_zero:
            buy_dir = liquid & price_ok & ((delta_pct > 0) | (macd_bias > 0))
            sell_dir = liquid & price_ok & ((delta_pct < 0) | (macd_bias < 0))
            base_strength = mtf.clip(lower=0.0) + range_pct.clip(lower=0.0, upper=3.0) + macd_bias.abs().clip(lower=0.0, upper=2.0)
            rescued_buy = base_strength.where(buy_dir, 0.0).clip(lower=0.0)
            rescued_sell = base_strength.where(sell_dir, 0.0).clip(lower=0.0)
            rescued_buy = rescued_buy.where(rescued_buy <= 0.0, rescued_buy.clip(lower=min_score))
            rescued_sell = rescued_sell.where(rescued_sell <= 0.0, rescued_sell.clip(lower=min_score))
            work["ai_disp_buy_score"] = rescued_buy
            work["ai_disp_sell_score"] = rescued_sell
            work["score_buy"] = rescued_buy
            work["buy_score"] = rescued_buy
            work["score_sell"] = rescued_sell
            work["sell_score"] = rescued_sell
            work["score_total"] = rescued_buy - rescued_sell
            work["total_score"] = work["score_total"]
            work["final_score"] = work["score_total"]
            work["display_score"] = work["score_total"]
            work["ai_disp_total_score"] = work["score_total"]
            work["ai_disp_final_score"] = work["score_total"]

        # slope が全ゼロなら、同じ足の OHLC から候補抽出用の疑似slopeを作る。
        slope_all_zero = float(slope.abs().sum()) <= 0.000001
        if slope_all_zero:
            pseudo_slope = delta_pct.where(delta_pct.abs() > 0.0, macd_bias / 10.0)
            work["ai_disp_slope"] = pseudo_slope
            if "slope" in work.columns:
                work["slope"] = pseudo_slope
            if "slope_atr_scaled" in work.columns:
                work["slope_atr_scaled"] = pseudo_slope
            if "score_slope" in work.columns:
                work["score_slope"] = pseudo_slope

        if score_all_zero or slope_all_zero:
            try:
                rb = pd.to_numeric(work.get("ai_disp_buy_score", 0.0), errors="coerce").fillna(0.0)
                rs = pd.to_numeric(work.get("ai_disp_sell_score", 0.0), errors="coerce").fillna(0.0)
                ps = pd.to_numeric(work.get("ai_disp_slope", 0.0), errors="coerce").fillna(0.0)
                logger.warning(
                    "[SUMMARY AI ZERO SCORE RESCUE] applied rows=%s score_all_zero=%s slope_all_zero=%s buy_pos=%s sell_pos=%s slope_nonzero=%s min_vol=%s min_turnover=%s",
                    len(work),
                    score_all_zero,
                    slope_all_zero,
                    int((rb > 0).sum()),
                    int((rs > 0).sum()),
                    int((ps.abs() > 0).sum()),
                    min_vol,
                    min_turnover,
                )
            except Exception:
                pass

        return work.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY AI ZERO SCORE RESCUE] failed; returning original dataframe")
        return out


def install() -> bool:
    global _PATCHED, _ORIGINAL_FILTER, _ORIGINAL_ATTACH_DISPLAY
    if _PATCHED:
        return True
    ok_any = False
    try:
        import trading.entry.summary_ai.executor as executor

        cur = getattr(executor, "_filter_blocked_ai_ok_items", None)
        if callable(cur):
            if not getattr(cur, "_summary_ai_liq_rescue_patch", False):
                _ORIGINAL_FILTER = cur
                _patched_filter_blocked_ai_ok_items._summary_ai_liq_rescue_patch = True  # type: ignore[attr-defined]
                executor._filter_blocked_ai_ok_items = _patched_filter_blocked_ai_ok_items
            ok_any = True
        else:
            logger.warning("[SUMMARY AI LIQ RESCUE] target filter not callable")
    except Exception:
        logger.exception("[SUMMARY AI LIQ RESCUE] executor patch install failed")

    try:
        import trading.entry.summary_ai.candidates as candidates

        cur_attach = getattr(candidates, "attach_display_like_columns", None)
        if callable(cur_attach):
            if not getattr(cur_attach, "_summary_ai_zero_score_rescue_patch", False):
                _ORIGINAL_ATTACH_DISPLAY = cur_attach
                _patched_attach_display_like_columns._summary_ai_zero_score_rescue_patch = True  # type: ignore[attr-defined]
                candidates.attach_display_like_columns = _patched_attach_display_like_columns
            ok_any = True
        else:
            logger.warning("[SUMMARY AI ZERO SCORE RESCUE] target attach_display_like_columns not callable")
    except Exception:
        logger.exception("[SUMMARY AI ZERO SCORE RESCUE] candidate patch install failed")

    _PATCHED = bool(ok_any)
    logger.warning("[SUMMARY AI LIQ/ZERO SCORE RESCUE] installed ok=%s", _PATCHED)
    return _PATCHED


__all__ = ["install"]
