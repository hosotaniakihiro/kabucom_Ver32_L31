# ============================================================
# File   : core/startup/entry_price_improvement_patch.py
# Version: Ver02-BID-ASK-PRICE-IMPROVEMENT-NO-RECURSION
# ------------------------------------------------------------
# build_entry_order() の戻り値を runtime patch し、LIMIT価格を
# Bid/Ask レベルで補正する。
#
# 重要修正:
#   - entry_limit_passive_runtime_patch と相互に巻き直して
#     RecursionError になる問題を防止する。
#   - wrapper には *_original を保持し、既存チェーン内に自分が
#     いる場合は再ラップしない。
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_BUILD_ENTRY_ORDER = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _row_to_dict(row: Any) -> dict:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _get_tick_size(price: float) -> float:
    try:
        from utils_common import get_tick_size
        tick = float(get_tick_size(float(price)))
        if tick > 0:
            return tick
    except Exception:
        pass
    return 1.0


def _round_to_tick(price: float, *, side: str) -> float:
    tick = _get_tick_size(price)
    if tick <= 0:
        tick = 1.0
    if side == "BUY":
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick


def _get_board(symbol: str, row: dict | None = None) -> dict:
    row = row or {}
    bid = _safe_float(_first(row, ("bid_price", "bid", "best_bid", "BidPrice"), 0.0), 0.0)
    ask = _safe_float(_first(row, ("ask_price", "ask", "best_ask", "AskPrice"), 0.0), 0.0)
    bid_qty = _safe_float(_first(row, ("bid_qty", "best_bid_qty", "BidQty", "bid_volume"), 0.0), 0.0)
    ask_qty = _safe_float(_first(row, ("ask_qty", "best_ask_qty", "AskQty", "ask_volume"), 0.0), 0.0)

    if bid > 0 and ask > 0:
        return {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "source": "entry_row"}

    try:
        from utils_common import get_latest_bid_ask
        q = get_latest_bid_ask(symbol)
        if isinstance(q, dict):
            bid = _safe_float(q.get("bid_price") or q.get("bid") or q.get("best_bid") or q.get("BidPrice"), 0.0)
            ask = _safe_float(q.get("ask_price") or q.get("ask") or q.get("best_ask") or q.get("AskPrice"), 0.0)
            bid_qty = _safe_float(q.get("bid_qty") or q.get("best_bid_qty") or q.get("BidQty") or q.get("bid_volume"), 0.0)
            ask_qty = _safe_float(q.get("ask_qty") or q.get("best_ask_qty") or q.get("AskQty") or q.get("ask_volume"), 0.0)
        elif isinstance(q, (list, tuple)) and len(q) >= 2:
            bid = _safe_float(q[0], 0.0)
            ask = _safe_float(q[1], 0.0)
        if bid > 0 and ask > 0:
            return {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "source": "get_latest_bid_ask"}
    except Exception:
        logger.debug("[ENTRY PRICE IMPROVE] get_latest_bid_ask failed symbol=%s", symbol, exc_info=True)

    return {"bid": 0.0, "ask": 0.0, "bid_qty": 0.0, "ask_qty": 0.0, "source": "missing"}


def _is_contrarian(row: dict, detail: dict) -> tuple[bool, str]:
    values = []
    for src in (row, detail):
        for k in (
            "climax_reversal_exception",
            "contrarian_reversed",
            "contrarian_half_size",
            "reverse_half_size",
        ):
            try:
                if bool(src.get(k)):
                    return True, k
            except Exception:
                pass
        for k in ("climax_type", "reverse_reason", "price_source", "entry_type", "source"):
            try:
                v = str(src.get(k) or "").lower()
                if v:
                    values.append(v)
            except Exception:
                pass
    joined = "|".join(values)
    if "contrarian" in joined or "climax" in joined or "reversal" in joined:
        return True, joined[:120]
    return False, ""


def _is_strong_trend(row: dict, side: str) -> tuple[bool, str]:
    score_buy = _safe_float(_first(row, ("score_buy", "buy_score", "disp_buy_score", "buy"), 0.0), 0.0)
    score_sell = _safe_float(_first(row, ("score_sell", "sell_score", "disp_sell_score", "sell"), 0.0), 0.0)
    slope = _safe_float(_first(row, ("slope_atr_scaled", "score_slope", "disp_slope", "slope"), 0.0), 0.0)
    pc3 = _safe_float(_first(row, ("price_change_3", "change_3", "ret_3", "return_3"), 0.0), 0.0)
    pc5 = _safe_float(_first(row, ("price_change_5", "change_5", "ret_5", "return_5"), 0.0), 0.0)

    if abs(pc3) <= 1.0:
        pc3 *= 100.0
    if abs(pc5) <= 1.0:
        pc5 *= 100.0

    min_score = _env_float("ENTRY_PRICE_IMPROVE_STRONG_SCORE", 7.0)
    min_slope = _env_float("ENTRY_PRICE_IMPROVE_STRONG_SLOPE", 0.03)
    min_recent = _env_float("ENTRY_PRICE_IMPROVE_STRONG_RECENT_PCT", 0.0)

    if side == "BUY":
        ok = score_buy >= min_score and slope >= min_slope and pc3 >= min_recent and pc5 >= min_recent
        return ok, f"score_buy={score_buy:.3f},slope={slope:.6f},pc3={pc3:.3f},pc5={pc5:.3f}"

    ok = score_sell >= min_score and slope <= -min_slope and pc3 <= -min_recent and pc5 <= -min_recent
    return ok, f"score_sell={score_sell:.3f},slope={slope:.6f},pc3={pc3:.3f},pc5={pc5:.3f}"


def _improved_price(*, symbol: str, side: str, row: dict, detail: dict) -> tuple[float, str, str, dict]:
    board = _get_board(symbol, row)
    bid = _safe_float(board.get("bid"), 0.0)
    ask = _safe_float(board.get("ask"), 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return 0.0, "KEEP_ORIGINAL", "board_missing", board

    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    max_spread = _env_float("ENTRY_PRICE_IMPROVE_MAX_SPREAD_PCT", 0.15)
    if spread_pct > max_spread:
        return 0.0, "KEEP_ORIGINAL", f"spread_too_wide:{spread_pct:.4f}>{max_spread:.4f}", {**board, "spread_pct": spread_pct}

    tick = _get_tick_size(mid)
    passive_tick_offset = int(_env_float("ENTRY_PRICE_IMPROVE_PASSIVE_TICKS", 0.0))
    normal_tick_offset = int(_env_float("ENTRY_PRICE_IMPROVE_NORMAL_TICKS", 1.0))

    contrarian, contra_reason = _is_contrarian(row, detail)
    strong, strong_reason = _is_strong_trend(row, side)

    if side == "BUY":
        if contrarian:
            price = min(bid + tick * max(0, passive_tick_offset), ask)
            mode = "PASSIVE_BID"
            reason = f"contrarian:{contra_reason}"
        elif strong and _env_bool("ENTRY_PRICE_IMPROVE_ALLOW_AGGRESSIVE_STRONG_TREND", True):
            price = ask
            mode = "AGGRESSIVE_ASK"
            reason = f"strong_trend:{strong_reason}"
        else:
            price = min(bid + tick * max(0, normal_tick_offset), ask)
            mode = "BID_PLUS_TICK"
            reason = "normal_price_improve"
        return _round_to_tick(price, side="BUY"), mode, reason, {**board, "spread_pct": spread_pct, "tick": tick}

    if contrarian:
        price = max(ask - tick * max(0, passive_tick_offset), bid)
        mode = "PASSIVE_ASK"
        reason = f"contrarian:{contra_reason}"
    elif strong and _env_bool("ENTRY_PRICE_IMPROVE_ALLOW_AGGRESSIVE_STRONG_TREND", True):
        price = bid
        mode = "AGGRESSIVE_BID"
        reason = f"strong_trend:{strong_reason}"
    else:
        price = max(ask - tick * max(0, normal_tick_offset), bid)
        mode = "ASK_MINUS_TICK"
        reason = "normal_price_improve"
    return _round_to_tick(price, side="SELL"), mode, reason, {**board, "spread_pct": spread_pct, "tick": tick}


def _patch_order_result(result: dict, *, symbol: str, side: str, source: str, entry_row: Any) -> dict:
    if not _env_bool("ENTRY_PRICE_IMPROVEMENT_ENABLED", True):
        return result
    if not isinstance(result, dict) or not result.get("ok"):
        return result

    detail = result.get("detail")
    if not isinstance(detail, dict):
        return result

    order_type = str(detail.get("order_type") or "").upper()
    if order_type != "LIMIT":
        return result

    row = _row_to_dict(entry_row)
    side_n = _norm_side(side)
    symbol_n = _norm_symbol(symbol or _first(row, ("symbol", "code", "stock_code"), ""))
    if side_n not in {"BUY", "SELL"}:
        return result

    old_price = _safe_float(detail.get("price"), 0.0)
    new_price, mode, reason, board = _improved_price(symbol=symbol_n, side=side_n, row=row, detail=detail)
    if new_price <= 0:
        logger.info(
            "[ENTRY PRICE IMPROVE] KEEP symbol=%s side=%s source=%s old_price=%s mode=%s reason=%s board=%s",
            symbol_n,
            side_n,
            source,
            old_price,
            mode,
            reason,
            board,
        )
        detail["price_improve_mode"] = mode
        detail["price_improve_reason"] = reason
        return result

    detail["price_before_improve"] = old_price
    detail["price"] = new_price
    detail["base_price"] = new_price
    detail["price_source"] = "bid_ask_price_improvement"
    detail["price_improve_mode"] = mode
    detail["price_improve_reason"] = reason
    detail["price_improve_board"] = board
    detail["price_improved"] = True

    logger.warning(
        "[ENTRY PRICE IMPROVE] symbol=%s side=%s source=%s mode=%s old_price=%s new_price=%s bid=%s ask=%s spread_pct=%s reason=%s",
        symbol_n,
        side_n,
        source,
        mode,
        old_price,
        new_price,
        board.get("bid"),
        board.get("ask"),
        board.get("spread_pct"),
        reason,
    )
    return result


def _chain_contains(fn: Any, marker: str, *, limit: int = 20) -> bool:
    seen: set[int] = set()
    cur = fn
    for _ in range(limit):
        if not callable(cur):
            return False
        ident = id(cur)
        if ident in seen:
            return False
        seen.add(ident)
        if getattr(cur, marker, False):
            return True
        nxt = (
            getattr(cur, "_entry_price_improvement_original", None)
            or getattr(cur, "_entry_board_touch_original", None)
            or getattr(cur, "_original", None)
        )
        if nxt is None:
            return False
        cur = nxt
    return False


def _patched_build_entry_order(*args, **kwargs):
    if not callable(_ORIG_BUILD_ENTRY_ORDER):
        return {"ok": False, "reason": "PRICE_IMPROVE_ORIGINAL_MISSING", "detail": {}}

    result = _ORIG_BUILD_ENTRY_ORDER(*args, **kwargs)
    try:
        symbol = kwargs.get("symbol")
        side = kwargs.get("side")
        source = kwargs.get("source")
        entry_row = kwargs.get("entry_row")
        return _patch_order_result(result, symbol=symbol, side=side, source=source, entry_row=entry_row)
    except Exception:
        logger.exception("[ENTRY PRICE IMPROVE] patch failed; keep original result")
        return result


def _is_currently_wrapped() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        import trading.handlers.entry_order_builder as eob
        return bool(
            _chain_contains(getattr(ec, "build_entry_order", None), "_entry_price_improvement_patch")
            and _chain_contains(getattr(eob, "build_entry_order", None), "_entry_price_improvement_patch")
        )
    except Exception:
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD_ENTRY_ORDER
    try:
        import trading.handlers.entry_controller as ec
        import trading.handlers.entry_order_builder as eob

        old = getattr(eob, "build_entry_order", None)

        # 既にチェーン内に price improvement がある場合は再ラップしない。
        # 特に board_touch wrapper の内側に既存 price wrapper がある状態で
        # ここで再度 old=board_touch を掴むと board_touch <-> price の循環になる。
        if _chain_contains(old, "_entry_price_improvement_patch"):
            _INSTALLED = True
            logger.warning("[ENTRY PRICE IMPROVE] already present in wrapper chain -> skip rewrap")
            return True

        if not callable(old):
            logger.error("[ENTRY PRICE IMPROVE] target build_entry_order unavailable")
            return False

        _ORIG_BUILD_ENTRY_ORDER = old
        _patched_build_entry_order._entry_price_improvement_patch = True  # type: ignore[attr-defined]
        _patched_build_entry_order._entry_price_improvement_original = old  # type: ignore[attr-defined]
        eob.build_entry_order = _patched_build_entry_order
        ec.build_entry_order = _patched_build_entry_order
        _INSTALLED = True

        logger.warning(
            "[ENTRY PRICE IMPROVE] installed enabled=%s normal_ticks=%.0f passive_ticks=%.0f strong_score=%.2f strong_slope=%.4f max_spread=%.4f allow_aggressive_strong=%s no_recursion=True",
            _env_bool("ENTRY_PRICE_IMPROVEMENT_ENABLED", True),
            _env_float("ENTRY_PRICE_IMPROVE_NORMAL_TICKS", 1.0),
            _env_float("ENTRY_PRICE_IMPROVE_PASSIVE_TICKS", 0.0),
            _env_float("ENTRY_PRICE_IMPROVE_STRONG_SCORE", 7.0),
            _env_float("ENTRY_PRICE_IMPROVE_STRONG_SLOPE", 0.03),
            _env_float("ENTRY_PRICE_IMPROVE_MAX_SPREAD_PCT", 0.15),
            _env_bool("ENTRY_PRICE_IMPROVE_ALLOW_AGGRESSIVE_STRONG_TREND", True),
        )
        return True
    except Exception:
        logger.exception("[ENTRY PRICE IMPROVE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY PRICE IMPROVE] auto install failed")


__all__ = ["install"]
