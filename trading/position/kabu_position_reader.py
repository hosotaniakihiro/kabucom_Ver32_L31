# ============================================================
# File   : trading/position/kabu_position_reader.py
# Version: V1.2-KABU-CREDIT-POSITION-READER-READ-STATUS
# ------------------------------------------------------------
# kabu Station の建玉一覧から「信用建玉だけ」を読み、
# symbol -> position dict に正規化する。
#
# 重要:
#   現物はEXIT監視しない。
#   product=2 を優先して信用建玉だけ取得する。
#   念のため MarginTradeType / AccountType でも現物らしき行を除外する。
#   API正常応答0件とAPI失敗を区別するため LAST_READ_OK を公開する。
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable

logger = logging.getLogger(__name__)

LAST_READ_OK: bool = False
LAST_RAW_COUNT: int = 0
LAST_ERROR: str = ""
LAST_READ_AT: dt.datetime | None = None


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


def _fetch_positions_payload(*, product: int = 2) -> Any:
    base, token = _resolve_base_and_token()
    if not base or not token:
        raise RuntimeError("base/token unavailable")

    # product=2: 信用。現物はEXIT監視しないため product=0 は使わない。
    url = base.rstrip("/") + "/positions?" + urllib.parse.urlencode({"product": int(product)})
    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", token)

    with urllib.request.urlopen(req, timeout=3.0) as res:
        raw = res.read()
        if not raw:
            return []
        return json.loads(raw.decode("utf-8"))


def _is_credit_position(x: dict) -> bool:
    """現物をEXIT監視から除外し、信用建玉だけ通す。"""
    margin_trade_type = _pick(x, "MarginTradeType", "margin_trade_type")
    account_type = _pick(x, "AccountType", "account_type")
    product = _pick(x, "Product", "product")

    mt = str(margin_trade_type or "").strip()
    at = str(account_type or "").strip()
    pr = str(product or "").strip()

    # product=2 は信用。APIレスポンスにProductが入っている場合はこれを最優先。
    if pr and pr not in {"2", "信用", "MARGIN", "margin"}:
        return False

    # 現物を示す値は除外。
    joined = f"{mt} {at} {pr}".upper()
    if any(x in joined for x in ["CASH", "現物", "GENBUTSU", "PRODUCT=1"]):
        return False

    # 信用建玉は通常 MarginTradeType が入る。空なら現物混入の疑いとして除外。
    if not mt:
        return False

    return True


def read_kabu_open_positions() -> Dict[str, Dict[str, Any]]:
    global LAST_READ_OK, LAST_RAW_COUNT, LAST_ERROR, LAST_READ_AT

    LAST_READ_AT = dt.datetime.now()
    LAST_READ_OK = False
    LAST_RAW_COUNT = 0
    LAST_ERROR = ""

    try:
        payload = _fetch_positions_payload(product=2)
        LAST_READ_OK = True
    except Exception as e:
        LAST_ERROR = str(e)
        logger.warning("[KABU POSITION READER] request failed err=%s", e)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    raw_count = 0
    skipped_cash = 0
    skipped_qty = 0
    skipped_price = 0

    for x in _iter_items(payload):
        raw_count += 1

        if not _is_credit_position(x):
            skipped_cash += 1
            continue

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
            "_position_source": "KABU.positions.credit_only",
        }

    LAST_RAW_COUNT = raw_count

    logger.warning(
        "[KABU POSITION READER] scan product=2 read_ok=%s raw=%d credit_open=%d skipped_cash=%d skipped_qty=%d skipped_price=%d symbols=%s",
        LAST_READ_OK,
        raw_count,
        len(out),
        skipped_cash,
        skipped_qty,
        skipped_price,
        sorted(out.keys()),
    )
    return out


def get_last_read_status() -> dict:
    return {
        "ok": bool(LAST_READ_OK),
        "raw_count": int(LAST_RAW_COUNT or 0),
        "error": str(LAST_ERROR or ""),
        "read_at": LAST_READ_AT,
    }


__all__ = ["read_kabu_open_positions", "get_last_read_status"]
