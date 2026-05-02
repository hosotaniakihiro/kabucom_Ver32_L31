# ============================================================
# File   : trading/push/push_stream/normalize.py
# Version: Ver1.0-PUSH-STREAM-NORMALIZE
# ============================================================

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .constants import MAX_RAW_LOG_CHARS
from .runtime import _now, _safe_iso


def _normalize_symbol(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _truncate(s: str, n: int = MAX_RAW_LOG_CHARS) -> str:
    if not isinstance(s, str):
        s = str(s)
    return s if len(s) <= n else s[:n] + " ...<truncated>"


def _parse_message(message: Any) -> Dict[str, Any]:
    if isinstance(message, dict):
        return message

    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8", errors="ignore")

    if isinstance(message, str):
        s = message.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {"raw_message": s}

    return {"raw_message": str(message)}


def _normalize_push_row(payload: Dict[str, Any]) -> Dict[str, Any]:
    symbol = _normalize_symbol(
        payload.get("Symbol")
        or payload.get("symbol")
        or payload.get("Code")
        or payload.get("code")
    )

    event_time = (
        payload.get("CurrentPriceTime")
        or payload.get("current_price_time")
        or payload.get("TradingVolumeTime")
        or payload.get("trading_volume_time")
        or payload.get("BidTime")
        or payload.get("bid_time")
        or payload.get("AskTime")
        or payload.get("ask_time")
        or payload.get("PriceTime")
        or payload.get("price_time")
        or payload.get("Time")
        or payload.get("time")
    )
    event_time_iso = _safe_iso(event_time) or _safe_iso(_now())

    row = {
        "received_at": _safe_iso(_now()),
        "datetime": event_time_iso,
        "current_price_time": event_time_iso,

        "symbol": symbol,
        "symbolname": payload.get("SymbolName") or payload.get("symbolname") or payload.get("Name"),

        "current_price": _to_float(
            payload.get("CurrentPrice")
            or payload.get("current_price")
            or payload.get("Price")
        ),

        "trading_volume": _to_float(payload.get("TradingVolume") or payload.get("trading_volume") or payload.get("Volume")),
        "trading_value": _to_float(payload.get("TradingValue") or payload.get("trading_value") or payload.get("Value")),
        "vwap": _to_float(payload.get("VWAP") or payload.get("vwap")),

        "bid_time": _safe_iso(payload.get("BidTime") or payload.get("bid_time")),
        "ask_time": _safe_iso(payload.get("AskTime") or payload.get("ask_time")),

        "bid_price": _to_float(payload.get("BidPrice") or payload.get("bid_price")),
        "bid_qty": _to_float(payload.get("BidQty") or payload.get("bid_qty")),
        "ask_price": _to_float(payload.get("AskPrice") or payload.get("ask_price")),
        "ask_qty": _to_float(payload.get("AskQty") or payload.get("ask_qty")),

        "high_price": _to_float(payload.get("HighPrice") or payload.get("high_price")),
        "high_price_time": _safe_iso(payload.get("HighPriceTime") or payload.get("high_price_time")),
        "low_price": _to_float(payload.get("LowPrice") or payload.get("low_price")),
        "low_price_time": _safe_iso(payload.get("LowPriceTime") or payload.get("low_price_time")),
        "opening_price": _to_float(payload.get("OpeningPrice") or payload.get("opening_price")),
        "opening_price_time": _safe_iso(payload.get("OpeningPriceTime") or payload.get("opening_price_time")),

        "exchange": payload.get("Exchange") or payload.get("exchange"),
        "exchange_name": payload.get("ExchangeName") or payload.get("exchange_name"),
        "market": payload.get("Market") or payload.get("market"),

        "over_sell_qty": _to_float(payload.get("OverSellQty") or payload.get("over_sell_qty")),
        "under_buy_qty": _to_float(payload.get("UnderBuyQty") or payload.get("under_buy_qty")),

        "calc_source": "push_stream",
        "raw_json": json.dumps(payload, ensure_ascii=False, default=str),
    }

    for k in [
        "Sell1", "Sell2", "Sell3", "Sell4", "Sell5",
        "Buy1", "Buy2", "Buy3", "Buy4", "Buy5",
        "OverSellQty", "UnderBuyQty",
    ]:
        if k in payload:
            row[k] = payload.get(k)

    return row


def _is_order_book_like(row: Dict[str, Any]) -> bool:
    return any(k in row for k in [
        "Sell1", "Sell2", "Sell3", "Sell4", "Sell5",
        "Buy1", "Buy2", "Buy3", "Buy4", "Buy5",
        "OverSellQty", "UnderBuyQty",
    ])