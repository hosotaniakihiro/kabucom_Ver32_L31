# ============================================================
# File   : trading/position/kabu_position_reader.py
# Version: V1.0-KABU-POSITION-READER
# ------------------------------------------------------------
# kabu Station の建玉一覧を読み、symbol -> position dict に正規化する。
# APIキー/URL解決は subscription_manager.register_ops の既存実装を利用する。
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable

logger = logging.getLogger(__name__)


def _normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"2", "BUY", "LONG", "信用買", "現物買"}:
        return "BUY"
    if s in {"1", "SELL", "SHORT", "信用売", "現物売"}:
        return "SELL"
    return s


def _pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        try:
            if k in d and d.get(k) not in (None, ""):
                return d.get(k)
        except Exception:
            pass
    return default


def _resolve_base_and_token() -> tuple[str, str]:
    try:
        from trading.push.subscription_manager.register_ops import _resolve_api_key, _resolve_base_url

        base = str(_resolve_base_url() or "").strip().rstrip("/")
        token = str(_resolve_api_key() or "").strip()
        return base, token
    except Exception:
        logger.debug("[KABU POSITION READER] resolve failed", exc_info=True)
        return "", ""


def _iter_items(payload: Any) -> Iterable[dict]:
    if isinstance(payload, list):
        for x in payload:
            if isinstance(x, dict):
                yield x
        return
    if isinstance(payload, dict):
        for key in ("Positions", "positions", "Data", "data", "Result", "result"):
            v = payload.get(key)
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, dict):
                        yield x
                return
        if "Symbol" in payload or "symbol" in payload:
            yield payload


def _fetch_positions_payload() -> Any:
    base, token = _resolve_base_and_token()
    if not base or not token:
        logger.warning("[KABU POSITION READER] skipped base/token unavailable")
        return None

    url = base.rstrip("/") + "/positions?" + urllib.parse.urlencode({"product": 0})
    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", token)

    with urllib.request.urlopen(req, timeout=3.0) as res:
        raw = res.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))


def read_kabu_open_positions() -> Dict[str, Dict[str, Any]]:
    try:
        payload = _fetch_positions_payload()
    except Exception as e:
        logger.warning("[KABU POSITION READER] request failed err=%s", e)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    raw_count = 0
    skipped_qty = 0
    skipped_price = 0

    for x in _iter_items(payload):
        raw_count += 1
        symbol = _normalize_symbol(_pick(x, "Symbol", "symbol", "Code", "code"))
        if not symbol:
            continue

        qty = _safe_int(_pick(x, "LeavesQty", "HoldQty", "Qty", "Quantity", "qty", "quantity", default=0), 0)
        if qty <= 0:
            skipped_qty += 1
            continue

        avg_price = _safe_float(_pick(x, "Price", "AvgPrice", "AveragePrice", "avg_price", default=0.0), 0.0)
        current_price = _safe_float(_pick(x, "CurrentPrice", "current_price", "price", default=0.0), 0.0)
        entry_price = avg_price if avg_price > 0 else current_price
        if entry_price <= 0:
            skipped_price += 1
            continue

        out[symbol] = {
            "symbol": symbol,
            "symbolname": _pick(x, "SymbolName", "symbolname"),
            "side": _side(_pick(x, "Side", "side")),
            "qty": qty,
            "quantity": qty,
            "avg_price": avg_price,
            "entry_price": entry_price,
            "price": current_price,
            "current_price": current_price,
            "entry_time": dt.datetime.now(),
            "status": "OPEN",
            "exchange": _pick(x, "Exchange", "exchange", default=1) or 1,
            "margin_trade_type": _pick(x, "MarginTradeType", "margin_trade_type"),
            "account_type": _pick(x, "AccountType", "account_type"),
            "hold_id": _pick(x, "HoldID", "hold_id"),
            "execution_id": _pick(x, "ExecutionID", "execution_id"),
            "_position_source": "KABU.positions",
        }

    logger.warning(
        "[KABU POSITION READER] scan raw=%d open=%d skipped_qty=%d skipped_price=%d symbols=%s",
        raw_count,
        len(out),
        skipped_qty,
        skipped_price,
        sorted(out.keys()),
    )
    return out


__all__ = ["read_kabu_open_positions"]
