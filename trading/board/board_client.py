# ============================================================
# File   : trading/board/board_client.py
# Version: Ver03-KABU-BOARD-SNAPSHOT-CLIENT-429-4002006-THROTTLE
# ------------------------------------------------------------
# kabuステーション REST API /board/{symbol}@{exchange} から
# 複数段板を取得し、判定しやすい形に正規化する。
#
# Ver03:
#   - 429(API実行回数エラー) だけでなく、4002006(レジスト数エラー) でも
#     グローバルクールダウンを入れる
#   - cooldown中はRESTを叩かず、直近キャッシュがあれば返す
#   - SUMMARY AI / entry直前の候補一斉板確認によるREST過多を抑制
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import threading
import time
from typing import Any, Optional

import requests

from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"
DEFAULT_EXCHANGE = 1

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}

_CACHE_LOCK = threading.RLock()
_BOARD_CACHE: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_LAST_REST_CALL_TS = 0.0
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_REASON = ""
_LAST_COOLDOWN_LOG_TS = 0.0


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
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


def _cache_ttl_sec() -> float:
    return max(0.0, _env_float("BOARD_CLIENT_CACHE_TTL_SEC", 2.0))


def _min_interval_sec() -> float:
    return max(0.0, _env_float("BOARD_CLIENT_MIN_INTERVAL_SEC", 0.18))


def _cooldown_sec(reason: str = "") -> float:
    if "4002006" in str(reason) or "レジスト数エラー" in str(reason):
        return max(0.0, _env_float("BOARD_CLIENT_REGISTER_ERROR_COOLDOWN_SEC", 20.0))
    return max(0.0, _env_float("BOARD_CLIENT_429_COOLDOWN_SEC", 5.0))


def _cached_snapshot(key: tuple[str, int, int], now: float, *, allow_stale_on_cooldown: bool = False) -> Optional[dict[str, Any]]:
    ttl = _cache_ttl_sec()
    stale_ttl = max(ttl, _env_float("BOARD_CLIENT_429_STALE_CACHE_SEC", 10.0)) if allow_stale_on_cooldown else ttl
    if stale_ttl <= 0:
        return None
    with _CACHE_LOCK:
        item = _BOARD_CACHE.get(key)
        if not item:
            return None
        ts, data = item
        if now - ts <= stale_ttl:
            out = dict(data)
            out["cache_hit"] = True
            out["cache_age_sec"] = round(now - ts, 3)
            return out
    return None


def _store_cache(key: tuple[str, int, int], data: dict[str, Any]) -> None:
    if _cache_ttl_sec() <= 0:
        return
    with _CACHE_LOCK:
        _BOARD_CACHE[key] = (time.time(), dict(data))
        if len(_BOARD_CACHE) > 128:
            oldest = sorted(_BOARD_CACHE.items(), key=lambda kv: kv[1][0])[:32]
            for k, _v in oldest:
                _BOARD_CACHE.pop(k, None)


def _wait_for_rate_limit() -> None:
    global _LAST_REST_CALL_TS
    min_interval = _min_interval_sec()
    if min_interval <= 0:
        return
    with _CACHE_LOCK:
        now = time.time()
        wait = (_LAST_REST_CALL_TS + min_interval) - now
        if wait > 0:
            time.sleep(min(wait, 1.0))
        _LAST_REST_CALL_TS = time.time()


def _is_rate_error(status: int, body: Any) -> tuple[bool, str]:
    text = str(body)
    if int(status) == 429 or "API実行回数エラー" in text or "4001006" in text:
        return True, "429_api_rate"
    if int(status) == 400 and ("レジスト数エラー" in text or "4002006" in text):
        return True, "4002006_register_count"
    return False, ""


def _mark_cooldown(symbol: str, status: int, body: Any, reason: str) -> None:
    global _COOLDOWN_UNTIL, _COOLDOWN_REASON, _LAST_COOLDOWN_LOG_TS
    now = time.time()
    sec = _cooldown_sec(reason or body)
    if sec <= 0:
        return
    _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, now + sec)
    _COOLDOWN_REASON = reason or f"status_{status}"
    if now - _LAST_COOLDOWN_LOG_TS >= 1.0:
        _LAST_COOLDOWN_LOG_TS = now
        logger.warning(
            "[BOARD CLIENT] cooldown start symbol=%s status=%s reason=%s cooldown=%.2fs body=%s",
            symbol,
            status,
            _COOLDOWN_REASON,
            max(0.0, _COOLDOWN_UNTIL - now),
            body,
        )


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

    levels_i = int(levels)
    exchange_i = int(exchange)
    key = (symbol_n, exchange_i, levels_i)
    now = time.time()

    if _env_bool("BOARD_CLIENT_CACHE_ENABLED", True):
        cached = _cached_snapshot(key, now)
        if cached is not None:
            logger.debug("[BOARD CLIENT] cache hit symbol=%s age=%.3fs", symbol_n, cached.get("cache_age_sec", 0.0))
            return cached

    with _CACHE_LOCK:
        cooldown_until = float(_COOLDOWN_UNTIL or 0.0)
        cooldown_reason = str(_COOLDOWN_REASON or "")
    if cooldown_until > now:
        cached = _cached_snapshot(key, now, allow_stale_on_cooldown=True)
        if cached is not None:
            logger.warning(
                "[BOARD CLIENT] cooldown cache fallback symbol=%s remain=%.2fs reason=%s age=%.3fs",
                symbol_n,
                cooldown_until - now,
                cooldown_reason,
                cached.get("cache_age_sec", 0.0),
            )
            return cached
        logger.warning(
            "[BOARD CLIENT] cooldown active -> skip REST symbol=%s remain=%.2fs reason=%s",
            symbol_n,
            cooldown_until - now,
            cooldown_reason,
        )
        return None

    token = get_valid_token()
    if not token:
        logger.warning("[BOARD CLIENT] token missing symbol=%s", symbol_n)
        return None

    url = f"{API_URL}/board/{symbol_n}@{exchange_i}"
    headers = {"X-API-KEY": token}

    try:
        _wait_for_rate_limit()
        res = requests.get(url, headers=headers, timeout=float(timeout))
        if res.status_code != 200:
            try:
                body = res.json()
            except Exception:
                body = res.text
            logger.warning("[BOARD CLIENT] http_ng symbol=%s status=%s body=%s", symbol_n, res.status_code, body)
            is_rate, reason = _is_rate_error(int(res.status_code), body)
            if is_rate:
                _mark_cooldown(symbol_n, int(res.status_code), body, reason)
                cached = _cached_snapshot(key, time.time(), allow_stale_on_cooldown=True)
                if cached is not None:
                    return cached
            return None
        raw = res.json()
        if not isinstance(raw, dict):
            return None

        sell = _extract_ladder(raw, "SELL", levels=levels_i)
        buy = _extract_ladder(raw, "BUY", levels=levels_i)

        current_price = _safe_float(raw.get("CurrentPrice") or raw.get("current_price"), 0.0)
        current_time = raw.get("CurrentPriceTime") or raw.get("current_price_time") or ""
        over_sell_qty = _safe_float(raw.get("OverSellQty") or raw.get("over_sell_qty"), 0.0)
        under_buy_qty = _safe_float(raw.get("UnderBuyQty") or raw.get("under_buy_qty"), 0.0)

        best_ask = sell[0]["price"] if sell else 0.0
        best_ask_qty = sell[0]["qty"] if sell else 0.0
        best_bid = buy[0]["price"] if buy else 0.0
        best_bid_qty = buy[0]["qty"] if buy else 0.0

        data = {
            "symbol": symbol_n,
            "exchange": exchange_i,
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
            "cache_hit": False,
        }
        _store_cache(key, data)
        return data
    except Exception:
        logger.debug("[BOARD CLIENT] fetch failed symbol=%s", symbol_n, exc_info=True)
        return None


__all__ = ["fetch_board_snapshot"]
