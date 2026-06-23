# ============================================================
# force_cancel_loop.py（BUY_SELL 準拠・安全最終版）
# Version: V2.3-ORDERS-429-BACKOFF
# ------------------------------------------------------------
# ・30秒ごとに kabusapi/orders を直接確認
# ・未約定の指値注文を全キャンセル
# ・global_data / pending_entries に依存しない
# ・401 / 429 / timeout / 切断耐性あり
# ・起動直後の API TOKEN 未設定にも耐性あり
# ・401時は token refresh 後に1回だけ再試行
# ・429時は token refresh せず、backoffしてAPI連打を止める
#
# V2.3:
# ・get_orders にプロセス内レート制限を追加。
# ・429 Too Many Requests は認証エラーではないため refresh_token() へ流さない。
# ・settings.ini [API_RATE_LIMIT] から間隔/backoffを調整可能。
# ============================================================

from __future__ import annotations

import configparser
import logging
import os
import threading
import time

import requests
from requests.exceptions import ConnectionError, HTTPError, ReadTimeout

from kabu_api.api_common import get_headers

logger = logging.getLogger(__name__)

BASE_URL = "http://localhost:18080/kabusapi"

# ------------------------------------------------------------
# settings.ini
# ------------------------------------------------------------
conf = configparser.ConfigParser()
conf.read(["settings.ini", "config/settings.ini"], encoding="utf-8-sig")
PASSWORD = conf.get("aukabu", "password", fallback="")


def _conf_get_float(section: str, key: str, default: float) -> float:
    """settings.ini の値を安全に float として読む。"""
    env_key = f"KABU_{key}".upper()
    try:
        v = os.getenv(env_key)
        if v is not None and str(v).strip() != "":
            return float(v)
    except Exception:
        pass

    for sec in (section, section.lower(), section.upper()):
        try:
            if conf.has_section(sec):
                v = conf.get(sec, key, fallback=None)
                if v is not None and str(v).strip() != "":
                    return float(v)
        except Exception:
            pass
    return float(default)


# ------------------------------------------------------------
# kabu API rate limit
# ------------------------------------------------------------
_GET_ORDERS_MIN_INTERVAL_SEC = max(
    0.0,
    _conf_get_float("API_RATE_LIMIT", "kabu_get_orders_min_interval_sec", 3.0),
)
_KABU_429_BACKOFF_SEC = max(
    1.0,
    _conf_get_float("API_RATE_LIMIT", "kabu_429_backoff_sec", 5.0),
)
_TOKEN_REFRESH_MIN_INTERVAL_SEC = max(
    3.0,
    _conf_get_float("API_RATE_LIMIT", "token_refresh_min_interval_sec", 30.0),
)

_GET_ORDERS_LOCK = threading.Lock()
_LAST_GET_ORDERS_MONO = 0.0

# ------------------------------------------------------------
# Cancel 可能 State
# kabuS API:
# 1=Received, 2=Accepted, 3=Working, 4=PartiallyContracted
# ------------------------------------------------------------
CANCELABLE_STATES = {1, 2, 3, 4}
_LAST_TOKEN_WARN_AT = 0.0
_LAST_401_REFRESH_AT = 0.0
_LAST_REFRESH_ERROR_WARN_AT = 0.0
_LAST_429_WARN_AT = 0.0


def _rate_limit_get_orders() -> None:
    """プロセス内の /orders 連打を抑制する。"""
    global _LAST_GET_ORDERS_MONO
    if _GET_ORDERS_MIN_INTERVAL_SEC <= 0:
        return

    with _GET_ORDERS_LOCK:
        now = time.monotonic()
        wait = _GET_ORDERS_MIN_INTERVAL_SEC - (now - _LAST_GET_ORDERS_MONO)
        if wait > 0:
            time.sleep(wait)
        _LAST_GET_ORDERS_MONO = time.monotonic()


def _retry_after_seconds(response) -> float:
    try:
        raw = response.headers.get("Retry-After")
        if raw is not None and str(raw).strip() != "":
            return max(float(raw), _KABU_429_BACKOFF_SEC)
    except Exception:
        pass
    return _KABU_429_BACKOFF_SEC


def _handle_429(context: str, response=None) -> None:
    """429はtoken不一致ではなく呼び出し過多。refreshせずbackoffする。"""
    global _LAST_429_WARN_AT
    sleep_sec = _retry_after_seconds(response) if response is not None else _KABU_429_BACKOFF_SEC
    now = time.time()
    if now - _LAST_429_WARN_AT >= 5.0:
        logger.warning(
            "[FORCE_CANCEL] %s got 429 Too Many Requests -> backoff %.1fs; token refresh skipped",
            context,
            sleep_sec,
        )
        _LAST_429_WARN_AT = now
    time.sleep(sleep_sec)


# ============================================================
# API TOKEN 準備確認
# ============================================================

def _sync_global_token(token: str | None) -> None:
    if not token:
        return
    try:
        from global_state import global_data
        for name in ("token_value", "API_TOKEN", "api_token", "token", "kabu_api_token"):
            try:
                setattr(global_data, name, token)
            except Exception:
                pass
    except Exception:
        pass
    try:
        import token_manager
        try:
            token_manager.API_TOKEN = token
        except Exception:
            pass
    except Exception:
        pass


def _read_global_token() -> str | None:
    try:
        from global_state import global_data
        for name in ("token_value", "API_TOKEN", "api_token", "token", "kabu_api_token"):
            try:
                token = getattr(global_data, name, None)
                if token:
                    return str(token)
            except Exception:
                pass
    except Exception:
        pass
    return None


def _headers_from_existing_token(context: str):
    """startup_config 等で既に同期されたtokenを使う。401後の最優先にはしない。"""
    token = _read_global_token()
    if not token:
        try:
            import token_manager
            token = getattr(token_manager, "API_TOKEN", None)
        except Exception:
            token = None
    if token:
        token = str(token)
        _sync_global_token(token)
        logger.warning("[FORCE_CANCEL] API TOKEN reused from existing runtime token context=%s", context)
        return {"X-API-KEY": token, "Content-Type": "application/json"}
    return None


def _direct_token_fallback(context: str = "unknown"):
    """api_common 経由で取れない時の最後の保険。ini必須エラーは抑制する。"""
    headers = _headers_from_existing_token(context)
    if headers is not None:
        return headers
    try:
        import token_manager
        token = getattr(token_manager, "API_TOKEN", None)
        if not token:
            try:
                token = token_manager.get_valid_token()
            except Exception as e:
                # settings.ini に [trade] だけの運用ではここが ValueError になる。
                logger.debug("[FORCE_CANCEL] token_manager.get_valid_token unavailable context=%s err=%s", context, e)
                token = None
        if token:
            token = str(token)
            _sync_global_token(token)
            return {"X-API-KEY": token, "Content-Type": "application/json"}
    except Exception:
        logger.debug("[FORCE_CANCEL] direct token fallback failed context=%s", context, exc_info=True)
    return None


def _refresh_headers_after_401(context: str):
    """401時に token を再取得し、global_data/api_common へ同期する。"""
    global _LAST_401_REFRESH_AT, _LAST_REFRESH_ERROR_WARN_AT
    now = time.time()

    if now - _LAST_401_REFRESH_AT < _TOKEN_REFRESH_MIN_INTERVAL_SEC:
        logger.warning("[FORCE_CANCEL] 401 refresh throttled context=%s", context)
        return _direct_token_fallback(context)
    _LAST_401_REFRESH_AT = now

    # 401を返したtokenは stale の可能性が高いので、既存token再利用より refresh を優先する。
    try:
        import token_manager
        token = token_manager.refresh_token()
        if token:
            token = str(token)
            _sync_global_token(token)
            logger.warning("[FORCE_CANCEL] API TOKEN refreshed after 401 context=%s", context)
            return {"X-API-KEY": token, "Content-Type": "application/json"}
    except Exception as e:
        # API ini未配置/一時失敗は想定内。ERROR tracebackを連発せず、既存token fallbackへ落とす。
        if now - _LAST_REFRESH_ERROR_WARN_AT >= 30.0:
            logger.warning("[FORCE_CANCEL] token refresh after 401 unavailable context=%s err=%s", context, e)
            _LAST_REFRESH_ERROR_WARN_AT = now
    return _direct_token_fallback(context)


def _safe_get_headers(context):
    global _LAST_TOKEN_WARN_AT
    try:
        return get_headers()
    except RuntimeError as e:
        if "API TOKEN is not set" in str(e):
            headers = _direct_token_fallback(context)
            if headers is not None:
                logger.warning("[FORCE_CANCEL] API TOKEN restored by direct fallback context=%s", context)
                return headers
            now = time.time()
            if now - _LAST_TOKEN_WARN_AT >= 5.0:
                logger.warning("[FORCE_CANCEL] API TOKEN not ready; skip %s", context)
                _LAST_TOKEN_WARN_AT = now
            return None
        raise
    except Exception:
        headers = _direct_token_fallback(context)
        if headers is not None:
            logger.warning("[FORCE_CANCEL] API TOKEN restored after get_headers error context=%s", context)
            return headers
        logger.exception("[FORCE_CANCEL] get_headers failed; skip %s", context)
        return None


# ============================================================
# 注文キャンセル
# ============================================================

def cancel_order(order_id):
    headers = _safe_get_headers("cancel_order")
    if headers is None:
        return False

    payload = {"OrderId": order_id, "Password": PASSWORD}

    for attempt in (1, 2):
        try:
            r = requests.put(
                f"{BASE_URL}/cancelorder",
                headers=headers,
                json=payload,
                timeout=(2, 5),
            )
            if r.status_code == 429:
                _handle_429("cancel_order", r)
                return False
            if r.status_code == 401 and attempt == 1:
                logger.warning("[FORCE_CANCEL] cancel_order got 401 -> refresh token and retry order_id=%s", order_id)
                headers = _refresh_headers_after_401("cancel_order")
                if headers is None:
                    return False
                continue
            r.raise_for_status()
            logger.warning("[FORCE_CANCEL] order_id=%s status=%s body=%s", order_id, r.status_code, r.text)
            return True
        except HTTPError as e:
            status = e.response.status_code if e.response is not None else "N/A"
            if status == 429:
                _handle_429("cancel_order", e.response)
            else:
                logger.error("[FORCE_CANCEL] HTTP error order_id=%s status=%s", order_id, status)
            return False
        except Exception:
            logger.exception("[FORCE_CANCEL] unexpected error order_id=%s", order_id)
            return False
    return False


# ============================================================
# kabu API : 注文取得
# ============================================================

def _parse_orders_response(data):
    if isinstance(data, dict):
        return data.get("Orders", []) or data.get("orders", []) or []
    if isinstance(data, list):
        return data
    return []


def get_orders():
    headers = _safe_get_headers("get_orders")
    if headers is None:
        return []

    for attempt in (1, 2):
        try:
            _rate_limit_get_orders()
            r = requests.get(
                f"{BASE_URL}/orders",
                headers=headers,
                timeout=(2, 5),
            )
            if r.status_code == 429:
                _handle_429("get_orders", r)
                return []
            if r.status_code == 401 and attempt == 1:
                logger.warning("[FORCE_CANCEL] get_orders got 401 -> refresh token and retry")
                headers = _refresh_headers_after_401("get_orders")
                if headers is None:
                    return []
                continue
            r.raise_for_status()
            return _parse_orders_response(r.json())
        except HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429:
                _handle_429("get_orders", e.response)
            elif status == 401:
                logger.warning("[FORCE_CANCEL] kabu API Unauthorized (401) in get_orders after retry")
            else:
                logger.exception("❌ kabu API HTTP error in get_orders")
            return []
        except ReadTimeout:
            logger.warning("⚠ kabu API read timeout (get_orders)")
            return []
        except ConnectionError:
            logger.warning("⚠ kabu API connection error (get_orders)")
            return []
        except RuntimeError as e:
            if "API TOKEN is not set" in str(e):
                logger.warning("[FORCE_CANCEL] API TOKEN not ready; skip get_orders")
                return []
            logger.exception("❌ runtime error in get_orders")
            return []
        except Exception:
            logger.exception("❌ unexpected error in get_orders")
            return []
    return []


# ============================================================
# 強制キャンセルループ
# ============================================================

def start_force_cancel_loop(interval_sec=30):
    logger.warning(
        "🛑 FORCE CANCEL LOOP START (%ss) get_orders_min_interval=%.1fs backoff_429=%.1fs refresh_min_interval=%.1fs",
        interval_sec,
        _GET_ORDERS_MIN_INTERVAL_SEC,
        _KABU_429_BACKOFF_SEC,
        _TOKEN_REFRESH_MIN_INTERVAL_SEC,
    )

    while True:
        try:
            orders = get_orders()
            if not orders:
                time.sleep(interval_sec)
                continue

            for o in orders:
                order_id = o.get("OrderId") or o.get("ID")
                state = o.get("State")
                price = o.get("Price")
                cum = o.get("CumQty", 0)
                qty = o.get("OrderQty", 0)

                if not order_id:
                    continue

                is_limit = price not in (0, None)
                is_open = qty and cum < qty
                can_cancel = state in CANCELABLE_STATES

                if is_limit and is_open and can_cancel:
                    logger.warning("[FORCE_CANCEL] CANCEL order_id=%s state=%s %s/%s", order_id, state, cum, qty)
                    cancel_order(order_id)
                    time.sleep(0.3)
        except Exception:
            logger.exception("[FORCE_CANCEL LOOP ERROR]")

        time.sleep(interval_sec)
