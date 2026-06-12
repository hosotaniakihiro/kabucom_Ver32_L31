# ============================================================
# File   : trading/position/kabu_position_reader.py
# Version: V1.5-KABU-CREDIT-POSITION-READER-401-RETRY
# ------------------------------------------------------------
# kabu Station の建玉一覧から「信用建玉だけ」を読み、
# symbol -> position dict に正規化する。
#
# 重要:
#   現物はEXIT監視しない。
#   product=2 を優先して信用建玉だけ取得する。
#   API正常応答0件とAPI失敗を区別するため LAST_READ_OK を公開する。
#
# V1.4:
#   - 数量/価格パースを強化。カンマ、全角数字、空白、文字混在を吸収。
#   - HoldQty/LeavesQty が文字列 "1,000" 等の場合に 0 扱いされる問題を防ぐ。
#   - 全信用候補が skipped_qty の場合に qty_samples を status/log へ出す。
#
# V1.5:
#   - /positions?product=2 が 401 を返した場合、token_manager.refresh_token()
#     でAPIキーを再取得し、global_data / kabu_api.global_data / register_opsへ同期して1回だけ再試行する。
#   - それでも401の場合はERROR tracebackではなくWARNINGで空dictを返す。
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from typing import Any, Dict, Iterable, Sequence

logger = logging.getLogger(__name__)

LAST_READ_OK: bool = False
LAST_RAW_COUNT: int = 0
LAST_CREDIT_CANDIDATE_COUNT: int = 0
LAST_CREDIT_OPEN_COUNT: int = 0
LAST_SKIPPED_CASH: int = 0
LAST_SKIPPED_QTY: int = 0
LAST_SKIPPED_PRICE: int = 0
LAST_SAMPLE_KEYS: list[str] = []
LAST_QTY_SAMPLES: list[dict] = []
LAST_ERROR: str = ""
LAST_READ_AT: dt.datetime | None = None


QTY_KEYS: tuple[str, ...] = (
    "LeavesQty", "HoldQty", "Qty", "Quantity", "CurrentQty", "PositionQty", "OpenQty",
    "RepayableQty", "SettlementQty", "BalanceQty", "possess_qty", "hold_qty", "leaves_qty",
    "qty", "quantity", "current_qty", "position_qty", "open_qty", "repayable_qty", "balance_qty",
)

AVG_PRICE_KEYS: tuple[str, ...] = (
    "Price", "AvgPrice", "AveragePrice", "ExecutionPrice", "EntryPrice", "HoldPrice",
    "avg_price", "average_price", "execution_price", "entry_price", "hold_price",
)

CURRENT_PRICE_KEYS: tuple[str, ...] = (
    "CurrentPrice", "ValuationPrice", "MarketPrice", "price", "current_price", "valuation_price", "market_price",
)


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


def _number_text(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = unicodedata.normalize("NFKC", str(v)).strip()
        s = s.replace(",", "").replace("株", "").replace("円", "")
        s = s.replace("＋", "+").replace("−", "-").replace("－", "-")
        # "100株(内...)" のような文字混在を救う。
        m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
        return m.group(0) if m else ""
    except Exception:
        return ""


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        s = _number_text(v)
        if not s:
            return int(default)
        return int(float(s))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        s = _number_text(v)
        if not s:
            return float(default)
        return float(s)
    except Exception:
        return float(default)


def _side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"2", "02", "20", "BUY", "LONG", "B", "信用買", "買", "買建", "現物買", "BUY_CREDIT"}:
        return "BUY"
    if s in {"1", "01", "10", "SELL", "SHORT", "S", "信用売", "売", "売建", "現物売", "SELL_CREDIT"}:
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


def _iter_key_values(d: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(d, dict):
        for k, v in d.items():
            yield str(k), v
            if isinstance(v, (dict, list)):
                yield from _iter_key_values(v)
    elif isinstance(d, list):
        for x in d:
            if isinstance(x, (dict, list)):
                yield from _iter_key_values(x)


def _qty_debug_sample(d: dict) -> dict:
    out = {}
    wanted = {str(k).lower() for k in QTY_KEYS}
    for k, v in _iter_key_values(d):
        if str(k).lower() in wanted:
            out[str(k)] = v
    return out


def _pick_positive_int(d: dict, keys: Sequence[str], default: int = 0) -> int:
    wanted = {str(k).lower() for k in keys}
    best = 0
    for k, v in _iter_key_values(d):
        if str(k).lower() not in wanted:
            continue
        n = _safe_int(v, 0)
        if n > 0:
            return n
        best = max(best, n)
    return int(default if best <= 0 else best)


def _pick_positive_float(d: dict, keys: Sequence[str], default: float = 0.0) -> float:
    wanted = {str(k).lower() for k in keys}
    for k, v in _iter_key_values(d):
        if str(k).lower() not in wanted:
            continue
        f = _safe_float(v, 0.0)
        if f > 0:
            return f
    return float(default)


def _sync_token(token: str | None) -> None:
    if not token:
        return
    token = str(token).strip()
    if not token:
        return
    for module_name in ("global_state", "kabu_api.global_data", "token_manager"):
        try:
            if module_name == "global_state":
                from global_state import global_data as target  # type: ignore
            else:
                import importlib
                target = importlib.import_module(module_name)
            for name in ("token_value", "API_TOKEN", "api_token", "token", "kabu_api_token"):
                try:
                    setattr(target, name, token)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        from trading.push.subscription_manager import register_ops
        for name in ("_API_KEY", "API_KEY", "_api_key"):
            try:
                setattr(register_ops, name, token)
            except Exception:
                pass
    except Exception:
        pass


def _refresh_token_after_401() -> str:
    try:
        import token_manager
        token = token_manager.refresh_token()
        if token:
            token = str(token).strip()
            _sync_token(token)
            logger.warning("[KABU POSITION READER] token refreshed after 401 token_len=%s", len(token))
            return token
    except TypeError:
        pass
    except Exception as e:
        logger.warning("[KABU POSITION READER] refresh_token() failed after 401 err=%s", e)
    try:
        from pathlib import Path
        from configparser import ConfigParser
        import token_manager
        root = Path(__file__).resolve().parents[2]
        conf = ConfigParser()
        conf.read(str(root / "settings.ini"), encoding="utf-8")
        section = "aukabu" if conf.has_section("aukabu") else "kabusapi"
        api_password = conf.get(section, "apipassword", fallback="")
        if api_password:
            token = token_manager.refresh_token(api_password)
            if token:
                token = str(token).strip()
                _sync_token(token)
                logger.warning("[KABU POSITION READER] token refreshed with settings after 401 token_len=%s", len(token))
                return token
    except Exception as e:
        logger.warning("[KABU POSITION READER] refresh_token(settings) failed after 401 err=%s", e)
    return ""


def _resolve_base_and_token() -> tuple[str, str]:
    try:
        from trading.push.subscription_manager.register_ops import _resolve_api_key, _resolve_base_url
        base = str(_resolve_base_url() or "").strip().rstrip("/")
        token = str(_resolve_api_key() or "").strip()
        if token:
            _sync_token(token)
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


def _urlopen_positions(base: str, token: str, *, product: int) -> Any:
    url = base.rstrip("/") + "/positions?" + urllib.parse.urlencode({"product": int(product)})
    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", token)
    with urllib.request.urlopen(req, timeout=3.0) as res:
        raw = res.read()
        if not raw:
            return []
        return json.loads(raw.decode("utf-8"))


def _fetch_positions_payload(*, product: int = 2) -> Any:
    base, token = _resolve_base_and_token()
    if not base or not token:
        raise RuntimeError("base/token unavailable")
    try:
        return _urlopen_positions(base, token, product=product)
    except urllib.error.HTTPError as e:
        if getattr(e, "code", None) != 401:
            raise
        logger.warning("[KABU POSITION READER] got 401 -> refresh token and retry once")
        new_token = _refresh_token_after_401()
        if not new_token:
            raise
        try:
            return _urlopen_positions(base, new_token, product=product)
        except urllib.error.HTTPError as e2:
            if getattr(e2, "code", None) == 401:
                logger.warning("[KABU POSITION READER] still 401 after refresh -> skip this cycle")
                return []
            raise


def _is_credit_position(x: dict) -> bool:
    margin_trade_type = _pick(x, "MarginTradeType", "margin_trade_type")
    account_type = _pick(x, "AccountType", "account_type")
    product = _pick(x, "Product", "product")
    mt = str(margin_trade_type or "").strip()
    at = str(account_type or "").strip()
    pr = str(product or "").strip()
    if pr and pr not in {"2", "信用", "MARGIN", "margin"}:
        return False
    joined = f"{mt} {at} {pr}".upper()
    if any(x in joined for x in ["CASH", "現物", "GENBUTSU", "PRODUCT=1"]):
        return False
    if pr == "2":
        return True
    if mt:
        return True
    return False


def read_kabu_open_positions() -> Dict[str, Dict[str, Any]]:
    global LAST_READ_OK, LAST_RAW_COUNT, LAST_CREDIT_CANDIDATE_COUNT, LAST_CREDIT_OPEN_COUNT
    global LAST_SKIPPED_CASH, LAST_SKIPPED_QTY, LAST_SKIPPED_PRICE, LAST_SAMPLE_KEYS, LAST_QTY_SAMPLES
    global LAST_ERROR, LAST_READ_AT

    LAST_READ_AT = dt.datetime.now()
    LAST_READ_OK = False
    LAST_RAW_COUNT = 0
    LAST_CREDIT_CANDIDATE_COUNT = 0
    LAST_CREDIT_OPEN_COUNT = 0
    LAST_SKIPPED_CASH = 0
    LAST_SKIPPED_QTY = 0
    LAST_SKIPPED_PRICE = 0
    LAST_SAMPLE_KEYS = []
    LAST_QTY_SAMPLES = []
    LAST_ERROR = ""

    try:
        payload = _fetch_positions_payload(product=2)
        LAST_READ_OK = True
    except Exception as e:
        LAST_ERROR = str(e)
        logger.warning("[KABU POSITION READER] request failed err=%s", e)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    raw_count = skipped_cash = skipped_qty = skipped_price = credit_candidate_count = 0

    for x in _iter_items(payload):
        raw_count += 1
        if not LAST_SAMPLE_KEYS:
            try:
                LAST_SAMPLE_KEYS = sorted([str(k) for k in x.keys()])[:80]
            except Exception:
                LAST_SAMPLE_KEYS = []

        if not _is_credit_position(x):
            skipped_cash += 1
            continue

        credit_candidate_count += 1
        symbol = _normalize_symbol(_pick(x, "Symbol", "symbol", "Code", "code"))
        if not symbol:
            continue

        qty = _pick_positive_int(x, QTY_KEYS, 0)
        if qty <= 0:
            skipped_qty += 1
            if len(LAST_QTY_SAMPLES) < 5:
                LAST_QTY_SAMPLES.append({"symbol": symbol, "side": _pick(x, "Side", "side"), "qty_values": _qty_debug_sample(x)})
            continue

        avg_price = _pick_positive_float(x, AVG_PRICE_KEYS, 0.0)
        current_price = _pick_positive_float(x, CURRENT_PRICE_KEYS, 0.0)
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
    LAST_CREDIT_CANDIDATE_COUNT = credit_candidate_count
    LAST_CREDIT_OPEN_COUNT = len(out)
    LAST_SKIPPED_CASH = skipped_cash
    LAST_SKIPPED_QTY = skipped_qty
    LAST_SKIPPED_PRICE = skipped_price

    logger.warning(
        "[KABU POSITION READER] scan product=2 read_ok=%s raw=%d credit_candidates=%d credit_open=%d skipped_cash=%d skipped_qty=%d skipped_price=%d sample_keys=%s qty_samples=%s symbols=%s",
        LAST_READ_OK, raw_count, credit_candidate_count, len(out), skipped_cash, skipped_qty, skipped_price,
        LAST_SAMPLE_KEYS, LAST_QTY_SAMPLES, sorted(out.keys()),
    )
    return out


def get_last_read_status() -> dict:
    return {
        "ok": bool(LAST_READ_OK),
        "raw_count": int(LAST_RAW_COUNT or 0),
        "credit_candidate_count": int(LAST_CREDIT_CANDIDATE_COUNT or 0),
        "credit_open_count": int(LAST_CREDIT_OPEN_COUNT or 0),
        "skipped_cash": int(LAST_SKIPPED_CASH or 0),
        "skipped_qty": int(LAST_SKIPPED_QTY or 0),
        "skipped_price": int(LAST_SKIPPED_PRICE or 0),
        "sample_keys": list(LAST_SAMPLE_KEYS or []),
        "qty_samples": list(LAST_QTY_SAMPLES or []),
        "error": str(LAST_ERROR or ""),
        "read_at": LAST_READ_AT,
    }


__all__ = ["read_kabu_open_positions", "get_last_read_status"]
