# ==========================================================
# trading/handlers/entry_order_builder.py
# Ver1.1.0-FINAL-QTY_OVERRIDE-COMPAT
# ----------------------------------------------------------
# ✔ 注文条件（price / order_type / qty）を決定するだけ
# ✔ 副作用ゼロ（発注・global_state 操作なし）
# ✔ SUMMARY / RANKING / TONOSAMA 共通
# ✔ BUY のみ最小単元救済（QTY_ZERO 根絶）
# ✔ qty_override 対応（ENTRY_CONTROLLER 最新版互換）
# ==========================================================

from __future__ import annotations

import math
from typing import Dict, Any, Optional

from global_state import global_data
from utils_common import (
    get_latest_bid_ask,
    calculate_qty_by_budget,
    get_tick_size,
)

# ==========================================================
# 定数
# ==========================================================

ALLOW_MARKET_IF_BAD_BOARD = True
MAX_SPREAD_PCT_FOR_LIMIT = 0.30
MIN_ENTRY_QTY = 100   # ★ BUY 最小単元


# ==========================================================
# 共通結果フォーマット
# ==========================================================

def _ok(**kwargs) -> Dict[str, Any]:
    return {
        "ok": True,
        "reason": "OK",
        "detail": kwargs,
    }


def _ng(reason: str, **detail) -> Dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "detail": detail,
    }


# ==========================================================
# 価格丸め
# ==========================================================

def _round_price(price: float, side: str) -> float:
    tick = get_tick_size(price)
    if side == "BUY":
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick


# ==========================================================
# 5秒ブレイク判定
# ==========================================================

def five_sec_breakout(symbol: str, side: str) -> Optional[float]:
    df = getattr(global_data, "realtime_5s", {}).get(symbol)
    if df is None or len(df) < 3:
        return None

    prev = df.iloc[-2]
    last = df.iloc[-1]

    if side == "BUY" and last["high_price"] > prev["high_price"]:
        return float(last["high_price"])

    if side == "SELL" and last["low_price"] < prev["low_price"]:
        return float(last["low_price"])

    return None


# ==========================================================
# 🔥 注文条件ビルド（唯一の公開API）
# ==========================================================

def build_entry_order(
    *,
    symbol: str,
    side: str,
    source: str,
    entry_row: Dict[str, Any],
    qty_override: Optional[int] = None,   # ★ 追加（互換対応）
) -> Dict[str, Any]:

    price = None
    base_price = None
    spread_pct = None
    board = None

    side = side.upper()
    source = source.upper()

    # ======================================================
    # SUMMARY 起点
    # ======================================================
    if source == "SUMMARY_AI":

        board = get_latest_bid_ask(symbol)
        if board:
            bid = board.get("bid_price")
            ask = board.get("ask_price")

            if not bid or not ask or bid <= 0 or ask <= 0:
                return _ng("INVALID_BOARD", board=board)

            spread_pct = (ask - bid) / bid * 100
            base_price = bid if side == "SELL" else ask

            if ALLOW_MARKET_IF_BAD_BOARD and spread_pct > MAX_SPREAD_PCT_FOR_LIMIT:
                order_type = "MARKET"
            else:
                price = _round_price(base_price, side)
                order_type = "LIMIT"

        else:
            base_price = entry_row.get("close_price") or entry_row.get("vwap")
            if not base_price or base_price <= 0:
                return _ng("NO_PRICE_SOURCE")

            order_type = "MARKET"

    # ======================================================
    # RANKING / TONOSAMA
    # ======================================================
    else:
        base_price = five_sec_breakout(symbol, side)
        if not base_price:
            return _ng("NO_5S_BREAKOUT")

        price = _round_price(base_price, side)
        order_type = "STOP"

    # ======================================================
    # 流動性評価
    # ======================================================
    try:
        volume = float(entry_row.get("volume") or 0.0)
    except Exception:
        volume = 0.0

    effective_price = price if price else base_price
    trading_value = effective_price * volume if effective_price else 0.0

    if trading_value < 3_000_000:
        return _ng(
            "LOW_LIQUIDITY",
            trading_value=trading_value,
            price=effective_price,
            volume=volume,
        )

    # ======================================================
    # 数量計算（qty_override 優先）
    # ======================================================
    forced_min_qty = False

    if qty_override is not None:
        qty = int(qty_override)
        if qty <= 0:
            return _ng("INVALID_QTY_OVERRIDE", qty_override=qty_override)
    else:
        if trading_value < 8_000_000:
            qty = MIN_ENTRY_QTY
            forced_min_qty = True
        else:
            qty = calculate_qty_by_budget(effective_price)
            if qty <= 0 and side == "BUY":
                qty = MIN_ENTRY_QTY
                forced_min_qty = True

    if qty <= 0:
        return _ng(
            "QTY_ZERO",
            side=side,
            price=price,
            base_price=base_price,
            spread_pct=spread_pct,
            trading_value=trading_value,
        )

    # ======================================================
    # OK
    # ======================================================
    detail = {
        "order_type": order_type,
        "price": price,
        "base_price": base_price,
        "qty": qty,
        "spread_pct": spread_pct,
        "board": bool(board),
    }

    if qty_override is not None:
        detail["qty_override"] = True

    if forced_min_qty:
        detail["forced_min_qty"] = True
        detail["liquidity_class"] = "LOW"

    return _ok(**detail)