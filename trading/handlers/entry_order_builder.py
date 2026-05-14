# ==========================================================
# trading/handlers/entry_order_builder.py
# Ver1.5.0-FINAL-SUMMARY-5SEC-PRE-ENTRY-GUARD
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
# ✔ SUMMARY_AI 発注直前に5秒足の向きを確認
#   - BUY  : 直近5秒足が上方向でなければ止める
#   - SELL : 直近5秒足が下方向でなければ止める
#   - 5秒足が無い場合は既定では許可（環境変数で必須化可能）
# ✔ 未約定は pending_order_monitor 側で2秒取消
# ==========================================================

from __future__ import annotations

import datetime as dt
import math
import os
from typing import Dict, Any, Optional, Tuple

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
# SUMMARY_AI 5秒足 直前確認
# ----------------------------------------------------------
ENTRY_ORDER_5S_GUARD_ENABLED = str(os.getenv("ENTRY_ORDER_5S_GUARD_ENABLED", "1")).lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# True にすると、5秒足が無い銘柄は発注しない。
# 既定 True にすると起動直後やPUSH未登録銘柄で全停止しやすいため、既定は False。
ENTRY_ORDER_REQUIRE_5S_DATA = str(os.getenv("ENTRY_ORDER_REQUIRE_5S_DATA", "0")).lower() in {
    "1",
    "true",
    "yes",
    "y",
    "on",
}

ENTRY_ORDER_5S_MIN_BARS = int(float(os.getenv("ENTRY_ORDER_5S_MIN_BARS", "2")))
ENTRY_ORDER_5S_MAX_AGE_SEC = float(os.getenv("ENTRY_ORDER_5S_MAX_AGE_SEC", "20"))

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
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
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


def _row_get(row: Any, *names: str, default: Any = None) -> Any:
    """dict / pandas Series / object の列名ゆらぎを吸収する。"""
    for name in names:
        try:
            if isinstance(row, dict) and name in row:
                v = row.get(name)
                if v not in (None, ""):
                    return v
            if hasattr(row, "get"):
                v = row.get(name, None)
                if v not in (None, ""):
                    return v
            if hasattr(row, name):
                v = getattr(row, name)
                if v not in (None, ""):
                    return v
        except Exception:
            continue
    return default


def _parse_dt(v: Any) -> Optional[dt.datetime]:
    try:
        if v is None or v == "":
            return None
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None) if v.tzinfo else v
        s = str(v).strip()
        if not s:
            return None
        s = s.replace("T", " ").split("+", 1)[0]
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


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
# SUMMARY_AI 5秒足 直前確認
# ==========================================================

def _get_5s_df(symbol: str):
    try:
        m = getattr(global_data, "realtime_5s", None)
        if isinstance(m, dict):
            return m.get(str(symbol)) or m.get(symbol) or m.get(str(symbol).zfill(4))
    except Exception:
        pass

    # 互換: 他の名前で保持されている場合も拾う。
    for attr in (
        "df_5s_by_symbol",
        "realtime_5sec",
        "realtime_5sec_bars",
        "five_sec_bars",
        "bars_5s",
    ):
        try:
            m = getattr(global_data, attr, None)
            if isinstance(m, dict):
                df = m.get(str(symbol)) or m.get(symbol) or m.get(str(symbol).zfill(4))
                if df is not None:
                    return df
        except Exception:
            continue
    return None


def _extract_5s_rows(df: Any) -> Optional[Tuple[Any, Any, int]]:
    try:
        if df is None:
            return None

        n = len(df)
        if n < max(2, ENTRY_ORDER_5S_MIN_BARS):
            return None

        # pandas DataFrame想定。
        if hasattr(df, "iloc"):
            prev = df.iloc[-2]
            last = df.iloc[-1]
            return prev, last, n

        # list[dict] / tuple[dict] 想定。
        if isinstance(df, (list, tuple)):
            return df[-2], df[-1], n

        return None
    except Exception:
        return None


def _five_sec_pre_entry_guard(symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    """
    SUMMARY_AIの発注直前に5秒足の向きを確認する。

    BUY許可:
      - 直近5秒足が陽線寄り
      - かつ、前5秒足より下がっていない、または高値更新

    SELL許可:
      - 直近5秒足が陰線寄り
      - かつ、前5秒足より上がっていない、または安値更新

    5秒足が無い場合:
      - ENTRY_ORDER_REQUIRE_5S_DATA=1 のときだけ止める
      - 既定は止めない
    """
    if not ENTRY_ORDER_5S_GUARD_ENABLED:
        return None

    source_u = str(source or "").upper()
    if source_u != "SUMMARY_AI":
        return None

    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None

    df = _get_5s_df(symbol)
    rows = _extract_5s_rows(df)
    if rows is None:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng(
                "FIVE_SEC_NO_DATA",
                symbol=symbol,
                side=side_u,
                require_5s=True,
                min_bars=ENTRY_ORDER_5S_MIN_BARS,
            )
        return None

    prev, last, nrows = rows

    prev_close = _safe_float(_row_get(prev, "close_price", "close", "price", "current_price", "Close", default=0.0), 0.0)
    prev_high = _safe_float(_row_get(prev, "high_price", "high", "High", default=0.0), 0.0)
    prev_low = _safe_float(_row_get(prev, "low_price", "low", "Low", default=0.0), 0.0)

    last_open = _safe_float(_row_get(last, "open_price", "open", "Open", default=0.0), 0.0)
    last_close = _safe_float(_row_get(last, "close_price", "close", "price", "current_price", "Close", default=0.0), 0.0)
    last_high = _safe_float(_row_get(last, "high_price", "high", "High", default=0.0), 0.0)
    last_low = _safe_float(_row_get(last, "low_price", "low", "Low", default=0.0), 0.0)

    ts = _row_get(last, "datetime", "timestamp", "time", "created_at", "updated_at", default=None)
    last_dt = _parse_dt(ts)
    age_sec = None
    if last_dt is not None:
        try:
            age_sec = abs((dt.datetime.now() - last_dt).total_seconds())
        except Exception:
            age_sec = None

    if age_sec is not None and age_sec > ENTRY_ORDER_5S_MAX_AGE_SEC:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng(
                "FIVE_SEC_STALE",
                symbol=symbol,
                side=side_u,
                age_sec=age_sec,
                max_age_sec=ENTRY_ORDER_5S_MAX_AGE_SEC,
                nrows=nrows,
            )
        return None

    # 最低限の価格が取れない場合。
    if last_close <= 0 or prev_close <= 0:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng(
                "FIVE_SEC_INVALID_PRICE",
                symbol=symbol,
                side=side_u,
                prev_close=prev_close,
                last_close=last_close,
                nrows=nrows,
            )
        return None

    if last_open <= 0:
        last_open = prev_close
    if last_high <= 0:
        last_high = max(last_open, last_close)
    if last_low <= 0:
        last_low = min(last_open, last_close)
    if prev_high <= 0:
        prev_high = prev_close
    if prev_low <= 0:
        prev_low = prev_close

    bullish = (last_close >= last_open) and ((last_close >= prev_close) or (last_high > prev_high))
    bearish = (last_close <= last_open) and ((last_close <= prev_close) or (last_low < prev_low))

    detail = {
        "symbol": symbol,
        "side": side_u,
        "nrows": nrows,
        "prev_close": prev_close,
        "last_open": last_open,
        "last_close": last_close,
        "prev_high": prev_high,
        "last_high": last_high,
        "prev_low": prev_low,
        "last_low": last_low,
        "age_sec": age_sec,
        "bullish": bullish,
        "bearish": bearish,
    }

    if side_u == "BUY" and not bullish:
        return _ng("FIVE_SEC_NOT_BULLISH", **detail)

    if side_u == "SELL" and not bearish:
        return _ng("FIVE_SEC_NOT_BEARISH", **detail)

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
    df = _get_5s_df(symbol)
    rows = _extract_5s_rows(df)
    if rows is None:
        return None

    prev, last, _nrows = rows
    prev_high = _safe_float(_row_get(prev, "high_price", "high", "High", default=0.0), 0.0)
    prev_low = _safe_float(_row_get(prev, "low_price", "low", "Low", default=0.0), 0.0)
    last_high = _safe_float(_row_get(last, "high_price", "high", "High", default=0.0), 0.0)
    last_low = _safe_float(_row_get(last, "low_price", "low", "Low", default=0.0), 0.0)

    if side == "BUY" and last_high > 0 and prev_high > 0 and last_high > prev_high:
        return float(last_high)

    if side == "SELL" and last_low > 0 and prev_low > 0 and last_low < prev_low:
        return float(last_low)

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

    five_sec_ng = _five_sec_pre_entry_guard(symbol=symbol, side=side, source=source)
    if five_sec_ng is not None:
        return five_sec_ng

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
        "five_sec_guard": bool(ENTRY_ORDER_5S_GUARD_ENABLED and source == "SUMMARY_AI"),
        "require_5s_data": ENTRY_ORDER_REQUIRE_5S_DATA,
    }

    if qty_override is not None:
        detail["qty_override"] = True

    if forced_min_qty:
        detail["forced_min_qty"] = True
        detail["liquidity_class"] = "LOW"

    return _ok(**detail)
