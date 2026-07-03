# ==========================================================
# trading/handlers/entry_order_builder.py
# Ver2.1.1-FIX-MISSING-LOGGER
# ----------------------------------------------------------
# ✔ 注文条件（price / order_type / qty）を決定するだけ
# ✔ 副作用ゼロ（発注・global_state 操作なし）
# ✔ SUMMARY / RANKING / TONOSAMA 共通
# ✔ BUY のみ最小単元救済（QTY_ZERO 根絶）
# ✔ qty_override 対応（ENTRY_CONTROLLER 最新版互換）
# ✔ SUMMARY_AI で板が無い場合は MARKET ではなく close/vwap 指値にする
# ✔ SUMMARY_AI の約定優先指値を導入
# ✔ 低ボラ最終防衛を安全化
# ✔ SUMMARY_AI 発注直前に5秒足の向きを確認
# ✔ SUMMARY_AI 発注直前にMTF/傾きの逆方向をブロック
# ✔ スプレッド上限を環境変数化し、既定0.15%へ厳格化
# ✔ SELL信用新規で base_price より下のLIMITを作らない
# ✔ Ver2.1.1: board retry logging で logger 未定義になり、
#              _execute_best_candidate が NameError で False になる問題を修正
#
# 【Ver2.0 追加】
# ✔ 低変動ガードを SUMMARY_AI 限定から RANKING / TONOSAMA にも拡張
# ✔ ENTRY_ORDER_LOW_MOVE_SOURCES で対象sourceを環境変数制御
# ✔ 変動の少ないランキング銘柄を発注直前で止める
#
# 【Ver2.1 追加】
# ✔ A/B PUSHローテーション対策: SUMMARY_AI 発注直前の板取得を短時間リトライ
# ✔ 板が一時的に無いだけで即 fallback/skip しにくくする
# ==========================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import time
from typing import Dict, Any, Optional, Tuple

from global_state import global_data
from trading.exit.symbol_trade_guard import is_entry_blocked
from utils_common import (
    get_latest_bid_ask,
    calculate_qty_by_budget,
    get_tick_size,
)

logger = logging.getLogger(__name__)

ALLOW_MARKET_IF_BAD_BOARD = str(os.getenv("ALLOW_MARKET_IF_BAD_BOARD", "1")).lower() not in {
    "0", "false", "no", "off",
}

MAX_SPREAD_PCT_FOR_LIMIT = float(
    os.getenv("MAX_SPREAD_PCT_FOR_LIMIT", os.getenv("ENTRY_ORDER_MAX_SPREAD_PCT", "0.15"))
)

MIN_ENTRY_QTY = 100
SUMMARY_AGGRESSIVE_LIMIT_TICKS = int(float(os.getenv("SUMMARY_AGGRESSIVE_LIMIT_TICKS", "1")))
SELL_CREDIT_LIMIT_DOWN_TICK_ENABLED = str(os.getenv("SELL_CREDIT_LIMIT_DOWN_TICK_ENABLED", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}
SELL_CREDIT_LIMIT_UP_TICK_WHEN_BOARD_MISSING = int(float(os.getenv("SELL_CREDIT_LIMIT_UP_TICK_WHEN_BOARD_MISSING", "0")))

ENTRY_ORDER_5S_GUARD_ENABLED = str(os.getenv("ENTRY_ORDER_5S_GUARD_ENABLED", "1")).lower() not in {
    "0", "false", "no", "off",
}
ENTRY_ORDER_REQUIRE_5S_DATA = str(os.getenv("ENTRY_ORDER_REQUIRE_5S_DATA", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}
ENTRY_ORDER_5S_MIN_BARS = int(float(os.getenv("ENTRY_ORDER_5S_MIN_BARS", "2")))
ENTRY_ORDER_5S_MAX_AGE_SEC = float(os.getenv("ENTRY_ORDER_5S_MAX_AGE_SEC", "20"))

ENTRY_ORDER_MTF_GUARD_ENABLED = str(os.getenv("ENTRY_ORDER_MTF_GUARD_ENABLED", "1")).lower() not in {
    "0", "false", "no", "off",
}
ENTRY_ORDER_REQUIRE_MTF_DATA = str(os.getenv("ENTRY_ORDER_REQUIRE_MTF_DATA", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}
ENTRY_ORDER_MTF_SLOPE_EPS = float(os.getenv("ENTRY_ORDER_MTF_SLOPE_EPS", "0.0"))
ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF = str(os.getenv("ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF", "1")).lower() not in {
    "0", "false", "no", "off",
}

ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED = str(os.getenv("ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED", "1")).lower() not in {
    "0", "false", "no", "off",
}
ENTRY_ORDER_LOW_MOVE_SOURCES = {
    s.strip().upper()
    for s in os.getenv("ENTRY_ORDER_LOW_MOVE_SOURCES", "SUMMARY_AI,RANKING,TONOSAMA,EARLY_SCALP").split(",")
    if s.strip()
}
ENTRY_ORDER_MIN_RANGE_PCT = float(os.getenv("ENTRY_ORDER_MIN_RANGE_PCT", "0.006"))
ENTRY_ORDER_MIN_ATR_RATIO = float(os.getenv("ENTRY_ORDER_MIN_ATR_RATIO", "0.0035"))
ENTRY_ORDER_REQUIRE_ATR = str(os.getenv("ENTRY_ORDER_REQUIRE_ATR", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}
ENTRY_ORDER_REQUIRE_HIGH_LOW = str(os.getenv("ENTRY_ORDER_REQUIRE_HIGH_LOW", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}

ENTRY_ORDER_BOARD_RETRY_ENABLED = str(os.getenv("ENTRY_ORDER_BOARD_RETRY_ENABLED", "1")).lower() not in {
    "0", "false", "no", "off",
}
ENTRY_ORDER_BOARD_RETRY_SEC = float(os.getenv("ENTRY_ORDER_BOARD_RETRY_SEC", "5.6"))
ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC = float(os.getenv("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.4"))
ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY = str(os.getenv("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", "0")).lower() in {
    "1", "true", "yes", "y", "on",
}


def _ok(**kwargs) -> Dict[str, Any]:
    return {"ok": True, "reason": "OK", "detail": kwargs}


def _ng(reason: str, **detail) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


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


def _safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


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


def _entry_get(entry_row: Any, *names: str, default: Any = None) -> Any:
    return _row_get(entry_row or {}, *names, default=default)


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


def _trade_guard_block(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        blocked, reason, detail = is_entry_blocked(symbol)
        if blocked:
            return _ng(reason, symbol=symbol, **(detail or {}))
    except Exception:
        return None
    return None


def _source_uses_low_move_guard(source: str) -> bool:
    src = str(source or "").upper()
    if "ALL" in ENTRY_ORDER_LOW_MOVE_SOURCES:
        return True
    return src in ENTRY_ORDER_LOW_MOVE_SOURCES


def _get_board_with_retry(symbol: str, *, source: str, side: str) -> Optional[Dict[str, Any]]:
    """
    A/Bローテーションでは、対象銘柄が未登録側にいる瞬間だけ板が取れない。
    SUMMARY_AIの発注直前だけ短時間待って再取得し、即スキップを減らす。
    """
    attempts = 0
    deadline = time.monotonic() + max(0.0, ENTRY_ORDER_BOARD_RETRY_SEC)
    interval = max(0.05, ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC)

    while True:
        attempts += 1
        try:
            board = get_latest_bid_ask(symbol)
        except Exception:
            board = None

        if board:
            if attempts > 1:
                logger.info(
                    "[ENTRY ORDER BOARD RETRY] board recovered symbol=%s side=%s source=%s attempts=%d",
                    symbol,
                    side,
                    source,
                    attempts,
                )
            return board

        if not ENTRY_ORDER_BOARD_RETRY_ENABLED:
            return None

        if time.monotonic() >= deadline:
            logger.warning(
                "[ENTRY ORDER BOARD RETRY] board missing after retry symbol=%s side=%s source=%s attempts=%d retry_sec=%.3f",
                symbol,
                side,
                source,
                attempts,
                ENTRY_ORDER_BOARD_RETRY_SEC,
            )
            return None

        time.sleep(interval)


def _low_move_hard_block(entry_row: Dict[str, Any], *, symbol: str, source: str) -> Optional[Dict[str, Any]]:
    if not ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED:
        return None
    if not _source_uses_low_move_guard(source):
        return None

    row = entry_row or {}
    close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
    atr = _safe_float(_first(row, ("atr_1m", "atr", "ATR", "atr14", "atr_14"), 0.0), 0.0)

    if close <= 0:
        return _ng(
            "LOW_MOVE_NO_CLOSE",
            symbol=symbol,
            source=source,
            close=close,
            min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
            min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
        )

    range_checked = False
    if high > 0 and low > 0 and high >= low:
        range_checked = True
        range_value = high - low
        range_pct = range_value / close if close > 0 else 0.0
        if range_pct < ENTRY_ORDER_MIN_RANGE_PCT:
            return _ng(
                "LOW_MOVE_RANGE_TOO_SMALL",
                symbol=symbol,
                source=source,
                close=close,
                high=high,
                low=low,
                range_value=range_value,
                range_pct=range_pct,
                min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
            )
    elif ENTRY_ORDER_REQUIRE_HIGH_LOW:
        return _ng(
            "LOW_MOVE_NO_HIGH_LOW",
            symbol=symbol,
            source=source,
            close=close,
            high=high,
            low=low,
            min_range_pct=ENTRY_ORDER_MIN_RANGE_PCT,
            min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
        )

    if atr > 0:
        atr_ratio = atr / close if close > 0 else 0.0
        if atr_ratio < ENTRY_ORDER_MIN_ATR_RATIO:
            return _ng(
                "LOW_MOVE_ATR_TOO_SMALL",
                symbol=symbol,
                source=source,
                close=close,
                atr=atr,
                atr_ratio=atr_ratio,
                min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
            )
    elif ENTRY_ORDER_REQUIRE_ATR:
        return _ng(
            "LOW_MOVE_NO_ATR",
            symbol=symbol,
            source=source,
            close=close,
            atr=atr,
            range_checked=range_checked,
            min_atr_ratio=ENTRY_ORDER_MIN_ATR_RATIO,
        )

    return None


def _summary_mtf_direction_guard(entry_row: Dict[str, Any], *, symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not ENTRY_ORDER_MTF_GUARD_ENABLED:
        return None
    if str(source or "").upper() != "SUMMARY_AI":
        return None
    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None
    row = entry_row or {}
    eps = abs(float(ENTRY_ORDER_MTF_SLOPE_EPS or 0.0))
    mtf = _safe_float_or_none(_entry_get(row, "mtf", "score_mtf", "mtf_score", "score_mtf_merged"))
    slope = _safe_float_or_none(_entry_get(row, "slope_atr_scaled", "slope", "score_slope"))
    slope_1m = _safe_float_or_none(_entry_get(row, "slope_atr_scaled_1m", "slope_1m", "slope1m"))
    slope_3m = _safe_float_or_none(_entry_get(row, "slope_atr_scaled_3m", "slope_3m", "slope3m"))
    slope_5m = _safe_float_or_none(_entry_get(row, "slope_atr_scaled_5m", "slope_5m", "slope5m"))
    values = {"mtf": mtf, "slope": slope, "slope_1m": slope_1m, "slope_3m": slope_3m, "slope_5m": slope_5m}
    available = {k: v for k, v in values.items() if v is not None}
    if not available:
        if ENTRY_ORDER_REQUIRE_MTF_DATA:
            return _ng("MTF_NO_DATA", symbol=symbol, side=side_u, require_mtf=True)
        return None
    if side_u == "BUY":
        bad = {k: v for k, v in available.items() if v < -eps}
        if bad:
            return _ng("MTF_NOT_BUY_ALIGNED", symbol=symbol, side=side_u, bad=bad, values=values, eps=eps)
    else:
        if ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF:
            bad = {k: v for k, v in available.items() if k != "mtf" and v > eps}
        else:
            bad = {k: v for k, v in available.items() if v > eps}
        if bad:
            return _ng("MTF_NOT_SELL_ALIGNED", symbol=symbol, side=side_u, bad=bad, values=values, eps=eps)
    return None


def _get_5s_df(symbol: str):
    try:
        m = getattr(global_data, "realtime_5s", None)
        if isinstance(m, dict):
            return m.get(str(symbol)) or m.get(symbol) or m.get(str(symbol).zfill(4))
    except Exception:
        pass
    for attr in ("df_5s_by_symbol", "realtime_5sec", "realtime_5sec_bars", "five_sec_bars", "bars_5s"):
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
        if hasattr(df, "iloc"):
            return df.iloc[-2], df.iloc[-1], n
        if isinstance(df, (list, tuple)):
            return df[-2], df[-1], n
    except Exception:
        return None
    return None


def _five_sec_pre_entry_guard(symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not ENTRY_ORDER_5S_GUARD_ENABLED:
        return None
    if str(source or "").upper() != "SUMMARY_AI":
        return None
    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None
    rows = _extract_5s_rows(_get_5s_df(symbol))
    if rows is None:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng("FIVE_SEC_NO_DATA", symbol=symbol, side=side_u, require_5s=True, min_bars=ENTRY_ORDER_5S_MIN_BARS)
        return None
    prev, last, nrows = rows
    prev_close = _safe_float(_row_get(prev, "close_price", "close", "price", "current_price", "Close", default=0.0), 0.0)
    prev_high = _safe_float(_row_get(prev, "high_price", "high", "High", default=0.0), 0.0)
    prev_low = _safe_float(_row_get(prev, "low_price", "low", "Low", default=0.0), 0.0)
    last_open = _safe_float(_row_get(last, "open_price", "open", "Open", default=0.0), 0.0)
    last_close = _safe_float(_row_get(last, "close_price", "close", "price", "current_price", "Close", default=0.0), 0.0)
    last_high = _safe_float(_row_get(last, "high_price", "high", "High", default=0.0), 0.0)
    last_low = _safe_float(_row_get(last, "low_price", "low", "Low", default=0.0), 0.0)
    last_dt = _parse_dt(_row_get(last, "datetime", "timestamp", "time", "created_at", "updated_at", default=None))
    age_sec = None
    if last_dt is not None:
        try:
            age_sec = abs((dt.datetime.now() - last_dt).total_seconds())
        except Exception:
            age_sec = None
    if age_sec is not None and age_sec > ENTRY_ORDER_5S_MAX_AGE_SEC:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng("FIVE_SEC_STALE", symbol=symbol, side=side_u, age_sec=age_sec, max_age_sec=ENTRY_ORDER_5S_MAX_AGE_SEC, nrows=nrows)
        return None
    if last_close <= 0 or prev_close <= 0:
        if ENTRY_ORDER_REQUIRE_5S_DATA:
            return _ng("FIVE_SEC_INVALID_PRICE", symbol=symbol, side=side_u, prev_close=prev_close, last_close=last_close, nrows=nrows)
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
    detail = {"symbol": symbol, "side": side_u, "nrows": nrows, "prev_close": prev_close, "last_open": last_open, "last_close": last_close, "prev_high": prev_high, "last_high": last_high, "prev_low": prev_low, "last_low": last_low, "age_sec": age_sec, "bullish": bullish, "bearish": bearish}
    if side_u == "BUY" and not bullish:
        return _ng("FIVE_SEC_NOT_BULLISH", **detail)
    if side_u == "SELL" and not bearish:
        return _ng("FIVE_SEC_NOT_BEARISH", **detail)
    return None


def _round_price(price: float, side: str) -> float:
    tick = get_tick_size(price)
    if side == "BUY":
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick


def _aggressive_limit_price(base_price: float, side: str, ticks: int = SUMMARY_AGGRESSIVE_LIMIT_TICKS) -> float:
    p = float(base_price)
    if p <= 0:
        return p
    side_u = str(side or "").upper()
    rounded = _round_price(p, side_u)
    tick = get_tick_size(rounded)
    n = max(0, int(ticks or 0))
    if side_u == "SELL" and not SELL_CREDIT_LIMIT_DOWN_TICK_ENABLED:
        up_ticks = max(0, int(SELL_CREDIT_LIMIT_UP_TICK_WHEN_BOARD_MISSING or 0))
        return rounded + tick * up_ticks
    if n <= 0:
        return rounded
    if side_u == "BUY":
        return rounded + tick * n
    return max(tick, rounded - tick * n)


def five_sec_breakout(symbol: str, side: str) -> Optional[float]:
    rows = _extract_5s_rows(_get_5s_df(symbol))
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


def build_entry_order(*, symbol: str, side: str, source: str, entry_row: Dict[str, Any], qty_override: Optional[int] = None) -> Dict[str, Any]:
    price = None
    base_price = None
    spread_pct = None
    board = None
    price_source = None
    side = side.upper()
    source = source.upper()

    trade_guard_ng = _trade_guard_block(symbol)
    if trade_guard_ng is not None:
        return trade_guard_ng

    low_move_ng = _low_move_hard_block(entry_row, symbol=symbol, source=source)
    if low_move_ng is not None:
        return low_move_ng

    mtf_ng = _summary_mtf_direction_guard(entry_row, symbol=symbol, side=side, source=source)
    if mtf_ng is not None:
        return mtf_ng

    five_sec_ng = _five_sec_pre_entry_guard(symbol=symbol, side=side, source=source)
    if five_sec_ng is not None:
        return five_sec_ng

    if source == "SUMMARY_AI":
        board = _get_board_with_retry(symbol, source=source, side=side)
        if board:
            bid = board.get("bid_price")
            ask = board.get("ask_price")
            if not bid or not ask or bid <= 0 or ask <= 0:
                if ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY:
                    return _ng("INVALID_BOARD", board=board, require_board=True)
                board = None
            else:
                spread_pct = (ask - bid) / bid * 100
                base_price = bid if side == "SELL" else ask
                price_source = "board_bid_ask_safe_limit"
                if ALLOW_MARKET_IF_BAD_BOARD and spread_pct > MAX_SPREAD_PCT_FOR_LIMIT:
                    return _ng("SPREAD_TOO_WIDE", symbol=symbol, side=side, spread_pct=spread_pct, max_spread_pct=MAX_SPREAD_PCT_FOR_LIMIT, bid=bid, ask=ask)
                price = _aggressive_limit_price(float(base_price), side)
                order_type = "LIMIT"
        if not board:
            if ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY:
                return _ng(
                    "STRICT_BOARD_MISSING",
                    symbol=symbol,
                    side=side,
                    source=source,
                    retry_enabled=ENTRY_ORDER_BOARD_RETRY_ENABLED,
                    retry_sec=ENTRY_ORDER_BOARD_RETRY_SEC,
                    message="板が取れないためSUMMARY_AI新規エントリーを停止",
                )
            base_price = entry_row.get("close_price") or entry_row.get("price") or entry_row.get("current_price") or entry_row.get("close") or entry_row.get("vwap")
            if not base_price or base_price <= 0:
                return _ng("NO_PRICE_SOURCE")
            price = _aggressive_limit_price(float(base_price), side)
            order_type = "LIMIT"
            price_source = "summary_fallback_safe_limit_after_board_retry"
    else:
        base_price = five_sec_breakout(symbol, side)
        if not base_price:
            return _ng("NO_5S_BREAKOUT")
        price = _round_price(base_price, side)
        order_type = "STOP"
        price_source = "five_sec_breakout"

    try:
        volume = float(entry_row.get("volume") or 0.0)
    except Exception:
        volume = 0.0
    effective_price = price if price else base_price
    trading_value = effective_price * volume if effective_price else 0.0
    if trading_value < 3_000_000:
        return _ng("LOW_LIQUIDITY", trading_value=trading_value, price=effective_price, volume=volume)

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
        return _ng("QTY_ZERO", side=side, price=price, base_price=base_price, spread_pct=spread_pct, trading_value=trading_value)

    detail = {
        "order_type": order_type,
        "price": price,
        "base_price": base_price,
        "qty": qty,
        "spread_pct": spread_pct,
        "max_spread_pct": MAX_SPREAD_PCT_FOR_LIMIT,
        "board": bool(board),
        "price_source": price_source,
        "aggressive_limit_ticks": SUMMARY_AGGRESSIVE_LIMIT_TICKS if source == "SUMMARY_AI" and order_type == "LIMIT" and side == "BUY" else 0,
        "sell_credit_limit_down_tick_enabled": SELL_CREDIT_LIMIT_DOWN_TICK_ENABLED,
        "trade_guard": True,
        "low_move_guard": bool(ENTRY_ORDER_LOW_MOVE_GUARD_ENABLED and _source_uses_low_move_guard(source)),
        "low_move_sources": sorted(ENTRY_ORDER_LOW_MOVE_SOURCES),
        "five_sec_guard": bool(ENTRY_ORDER_5S_GUARD_ENABLED and source == "SUMMARY_AI"),
        "require_5s_data": ENTRY_ORDER_REQUIRE_5S_DATA,
        "mtf_guard": bool(ENTRY_ORDER_MTF_GUARD_ENABLED and source == "SUMMARY_AI"),
        "require_mtf_data": ENTRY_ORDER_REQUIRE_MTF_DATA,
        "sell_ignore_positive_mtf": ENTRY_ORDER_SELL_IGNORE_POSITIVE_MTF,
        "board_retry_enabled": ENTRY_ORDER_BOARD_RETRY_ENABLED,
        "board_retry_sec": ENTRY_ORDER_BOARD_RETRY_SEC,
        "require_board_for_summary": ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY,
    }
    if qty_override is not None:
        detail["qty_override"] = True
    if forced_min_qty:
        detail["forced_min_qty"] = True
        detail["liquidity_class"] = "LOW"
    return _ok(**detail)
