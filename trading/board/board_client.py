# ============================================================
# File   : trading/board/board_client.py
# Version: Ver01-KABU-BOARD-SNAPSHOT-CLIENT
# ------------------------------------------------------------
# kabuステーション REST API /board/{symbol}@{exchange} から
# 複数段板を取得し、判定しやすい形に正規化する。
#
# 注意:
#   - 発注はしない。取得と整形だけ。
#   - token_manager.get_valid_token() を利用。
#   - 失敗時は None を返す。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Optional

import requests

from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"
DEFAULT_EXCHANGE = 1


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


def _norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _extract_ladder(raw: dict, side: str, levels: int = 10) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    prefix = "Sell" if side.upper() == "SELL" else "Buy"
    for i in range(1, int(levels) + 1):
        v = raw.get(f"{prefix}{i}")
        if not isinstance(v, dict):
            continue
        price = _safe_float(v.get("Price") or v.get("price") or v.get("気配値"), 0.0)
        qty = _safe_float(v.get("Qty") or v.get("qty") or v.get("Quantity") or v.get("数量"), 0.0)
        if price > 0:
            rows.append({"level": float(i), "price": price, "qty": max(0.0, qty)})
    return rows


def fetch_board_snapshot(
    symbol: str,
    *,
    exchange: int = DEFAULT_EXCHANGE,
    timeout: float = 1.0,
    levels: int = 10,
) -> Optional[dict[str, Any]]:
    symbol_n = _norm_symbol(symbol)
    if not symbol_n:
        return None

    token = get_valid_token()
    if not token:
        logger.warning("[BOARD CLIENT] token missing symbol=%s", symbol_n)
        return None

    url = f"{API_URL}/board/{symbol_n}@{int(exchange)}"
    headers = {"X-API-KEY": token}

    try:
        res = requests.get(url, headers=headers, timeout=float(timeout))
        if res.status_code != 200:
            try:
                body = res.json()
            except Exception:
                body = res.text
            logger.warning("[BOARD CLIENT] http_ng symbol=%s status=%s body=%s", symbol_n, res.status_code, body)
            return None

        raw = res.json()
        if not isinstance(raw, dict):
            return None

        sell = _extract_ladder(raw, "SELL", levels=levels)
        buy = _extract_ladder(raw, "BUY", levels=levels)

        current_price = _safe_float(raw.get("CurrentPrice") or raw.get("current_price"), 0.0)
        current_time = raw.get("CurrentPriceTime") or raw.get("current_price_time") or ""
        over_sell_qty = _safe_float(raw.get("OverSellQty") or raw.get("over_sell_qty"), 0.0)
        under_buy_qty = _safe_float(raw.get("UnderBuyQty") or raw.get("under_buy_qty"), 0.0)

        best_ask = sell[0]["price"] if sell else 0.0
        best_ask_qty = sell[0]["qty"] if sell else 0.0
        best_bid = buy[0]["price"] if buy else 0.0
        best_bid_qty = buy[0]["qty"] if buy else 0.0

        return {
            "symbol": symbol_n,
            "exchange": int(exchange),
            "fetched_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "current_price": current_price,
            "current_price_time": str(current_time or ""),
            "best_ask": best_ask,
            "best_ask_qty": best_ask_qty,
            "best_bid": best_bid,
            "best_bid_qty": best_bid_qty,
            "over_sell_qty": over_sell_qty,
            "under_buy_qty": under_buy_qty,
            "sell": sell,
            "buy": buy,
            "raw": raw,
        }
    except Exception:
        logger.debug("[BOARD CLIENT] fetch failed symbol=%s", symbol_n, exc_info=True)
        return None


__all__ = ["fetch_board_snapshot"]
