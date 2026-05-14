# ==========================================================
# trading/handlers/entry_order_builder.py
# Ver1.4.1-FINAL-SUMMARY-LOW-MOVE-SAFE-GUARD
# ----------------------------------------------------------
# ✔ 注文条件（price / order_type / qty）を決定するだけ
# ✔ 副作用ゼロ（発注・global_state 操作なし）
# ✔ SUMMARY / RANKING / TONOSAMA 共通
# ✔ BUY のみ最小単元救済（QTY_ZERO 根絶）
# ✔ qty_override 対応（ENTRY_CONTROLLER 最新版互換）
# ✔ SUMMARY_AI で板が無い場合は MARKET ではなく close/vwap 指値にする
# ✔ SUMMARY_AI の約定優先指値を導入
#   - BUY  : ask + 1tick / fallback価格 + 1tick
#   - SELL : bid - 1tick / fallback価格 - 1tick
# ✔ 低ボラ最終防衛を安全化
#   - high/low がある場合だけ値幅不足を止める
#   - ATR は存在する場合だけ判定する
#   - high/low/ATR 欠損だけで全停止しない
# ✔ 未約定は pending_order_monitor 側で2秒取消
# ==========================================================

from __future__ import annotations

import math
import os
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
MIN_ENTRY_QTY = 100

SUMMARY_AGGRESSIVE_LIMIT_TICKS = 1

# ----------------------------------------------------------
# 低ボラ最終防衛
# ----------------------------------------------------------
ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED = str(os.getenv("ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED", "1")).lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# 1.2% は強すぎて候補を落としすぎるため、まず 0.6% にする。
# さらに厳しくしたい場合は環境変数 ENTRY_ORDER_MIN_RANGE_PCT=0.012 で上げる。
ENTRY_ORDER_MIN_RANGE_PCT = float(os.getenv("ENTRY_ORDER_MIN_RANGE_PCT", "0.006"))

# ATR は存在する場合だけ判定する。
ENTRY_ORDER_MIN_ATR_RATIO = float(os.getenv("ENTRY_ORDER_MIN_ATR_RATIO", "0.0035"))

# 重要:
# True にすると、entry_row に atr が無いだけで全候補が止まりやすい。
# 現状 summary 候補は atr 欠損が多いため、既定は False。
ENTRY_ORDER_REQUIRE_ATR = str(os.getenv("ENTRY_ORDER_REQUIRE_ATR", "0")).lower() in {
    "1",
    "true",
    "yes",
    "y",
    "on",
}

# high/low が無い場合に止めるか。
# 現状 entry_diag に high/low が載らない経路があるため、既定は False。
ENTRY_ORDER_REQUIRE_HIGH_LOW = str(os.getenv("ENTRY_ORDER_REQUIRE_HIGH_LOW", "0")).lower() in {
    "1",
    "true",
    "yes",
    "y",
    "on",
}


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
# safe helpers
# ==========================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _first(row: Dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    try:
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
    except Exception:
        pass
    return default


# ==========================================================
# 低ボラ最終防衛
# ==========================================================

def _low_move_hard_block(entry_row: Dict[str, Any], *, symbol: str, source: str) -> Optional[Dict[str, Any]]:
    """
    発注直前の低ボラ最終防衛。

    Ver1.4.1 方針:
      - データがある場合だけ厳しく判定する。
      - high/low/atr が無いだけでは全停止させない。
      - 動いていないと数値で確認できる場合だけ止める。
    """
    if not ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED:
        return None

    source_u = str(source or "").upper()
    if source_u != "SUMMARY_AI":
        return None

    row = entry_row or {}

    close = _safe_float(
        _first(row, ("close_price", "close", "price", "current_price"), 0.0),
        0.0,
    )
    high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
    atr = _safe_float(
        _first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0),
        0.0,
    )

    if close <= 0:
        return _ng(
            "LOW_MOVE_NO_CLOSE",
            symbol=symbol,
            close=close,
            min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
            min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
        )

    # high/low がある場合だけ range 判定する。
    if high > 0 and low > 0 and high >= low:
        range_value = high - low
        range_pct = range_value / close if close > 0 else 0.0

        if range_pct < ENTRY_ORDER_MIN_RANGE_PCT:
            return _ng(
                "LOW_MOVE_RANGE_TOO_SMALL",
                symbol=symbol,
                close=close,
                high=high,
                low=low,
                range_value=range_value,
                range_pct=range_pct,
                min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
            )
    else:
        if ENTRY_ORDER_REQUIRE_HIGH_LOW:
            return _ng(
                "LOW_MOVE_NO_HIGH_LOW",
                symbol=symbol,
                close=close,
                high=high,
                low=low,
                min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
                min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
            )

    # ATR がある場合だけ ATR 判定する。
    if atr > 0:
        atr_ratio = atr / close if close > 0 else 0.0
        if atr_ratio < ENTRY_ORDER_MIN_ATR_RATIO:
            return _ng(
                "LOW_MOVE_ATR_TOO_SMALL",
                symbol=symbol,
                close=close,
                atr=atr,
                atr_ratio=atr_ratio,
                min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
            )
    else:
        if ENTRY_ORDER_REQUIRE_ATR:
            return _ng(
                "LOW_MOVE_NO_ATR",
                symbol=symbol,
                close=close,
                atr=atr,
                min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
            )

    return None


# ==========================================================
# 価格丸め
# ==========================================================

def _round_price(price: float, side: str) -> float:
    tick = get_tick_size(price)
    if side == "BUY":
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick


def _aggressive_limit_price(base_price: float, side: str, ticks: int = SUMMARY_AGGRESSIVE_LIMIT_TICKS) -> float:
    p = float(base_price)
    if p <= 0:
        return p

    rounded = _round_price(p, side)
    tick = get_tick_size(rounded)
    n = max(0, int(ticks or 0))

    if n <= 0:
        return rounded

    if side.upper() == "BUY":
        return rounded + tick * n

    return max(tick, rounded - tick * n)


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
# 注文条件ビルド（唯一の公開API）
# ==========================================================

def build_entry_order(
    *,
    symbol: str,
    side: str,
    source: str,
    entry_row: Dict[str, Any],
    qty_override: Optional[int] = None,
) -> Dict[str, Any]:

    price = None
    base_price = None
    spread_pct = None
    board = None
    price_source = None

    side = side.upper()
    source = source.upper()

    low_move_ng = _low_move_hard_block(entry_row, symbol=symbol, source=source)
    if low_move_ng is not None:
        return low_move_ng

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
            price_source = "board_bid_ask_aggressive_1tick"

            if ALLOW_MARKET_IF_BAD_BOARD and spread_pct > MAX_SPREAD_PCT_FOR_LIMIT:
                order_type = "MARKET"
                price = None
                price_source = "board_bad_spread_market"
            else:
                price = _aggressive_limit_price(float(base_price), side)
                order_type = "LIMIT"

        else:
            base_price = (
                entry_row.get("close_price")
                or entry_row.get("price")
                or entry_row.get("current_price")
                or entry_row.get("close")
                or entry_row.get("vwap")
            )
            if not base_price or base_price <= 0:
                return _ng("NO_PRICE_SOURCE")

            price = _aggressive_limit_price(float(base_price), side)
            order_type = "LIMIT"
            price_source = "summary_fallback_aggressive_1tick"

    # ======================================================
    # RANKING / TONOSAMA
    # ======================================================
    else:
        base_price = five_sec_breakout(symbol, side)
        if not base_price:
            return _ng("NO_5S_BREAKOUT")

        price = _round_price(base_price, side)
        order_type = "STOP"
        price_source = "five_sec_breakout"

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

    detail = {
        "order_type": order_type,
        "price": price,
        "base_price": base_price,
        "qty": qty,
        "spread_pct": spread_pct,
        "board": bool(board),
        "price_source": price_source,
        "aggressive_limit_ticks": SUMMARY_AGGRESSIVE_LIMIT_TICKS if source == "SUMMARY_AI" and order_type == "LIMIT" else 0,
        "low_move_guard": bool(ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED and source == "SUMMARY_AI"),
        "min_range_pct": ENTRY_ORDER_MIN_RANGE_PCT,
        "min_atr_ratio": ENTRY_ORDER_MIN_ATR_RATIO,
        "require_atr": ENTRY_ORDER_REQUIRE_ATR,
        "require_high_low": ENTRY_ORDER_REQUIRE_HIGH_LOW,
    }

    if qty_override is not None:
        detail["qty_override"] = True

    if forced_min_qty:
        detail["forced_min_qty"] = True
        detail["liquidity_class"] = "LOW"

    return _ok(**detail)
