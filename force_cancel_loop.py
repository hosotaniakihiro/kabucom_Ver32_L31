# ============================================================
# force_cancel_loop.py（BUY_SELL 準拠・安全最終版）
# ------------------------------------------------------------
# ・30秒ごとに kabusapi/orders を直接確認
# ・未約定の指値注文を全キャンセル
# ・global_data / pending_entries に依存しない
# ・401 / timeout / 切断耐性あり
# ・起動直後の API TOKEN 未設定にも耐性あり
# ============================================================

import time
import logging
import requests
import configparser
from requests.exceptions import ReadTimeout, ConnectionError, HTTPError

from kabu_api.api_common import get_headers

logger = logging.getLogger(__name__)

BASE_URL = "http://localhost:18080/kabusapi"

# ------------------------------------------------------------
# settings.ini
# ------------------------------------------------------------
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
PASSWORD = conf.get("aukabu", "password", fallback="")

# ------------------------------------------------------------
# Cancel 可能 State
# kabuS API:
# 1=Received, 2=Accepted, 3=Working, 4=PartiallyContracted
# ------------------------------------------------------------
CANCELABLE_STATES = {1, 2, 3, 4}
_LAST_TOKEN_WARN_AT = 0.0


# ============================================================
# API TOKEN 準備確認
# ============================================================

def _direct_token_fallback():
    """api_common 経由で取れない時の最後の保険。

    startup_config.refresh_token_safe が global_data.clear_all の前後で alias を失っても、
    token_manager.API_TOKEN / settings.ini token から復旧する。
    """
    try:
        import token_manager
        token = getattr(token_manager, "API_TOKEN", None)
        if not token:
            token = token_manager.get_valid_token()
        if token:
            try:
                from global_state import global_data
                for name in ("token_value", "API_TOKEN", "api_token", "token", "kabu_api_token"):
                    try:
                        setattr(global_data, name, token)
                    except Exception:
                        pass
            except Exception:
                pass
            return {"X-API-KEY": str(token), "Content-Type": "application/json"}
    except Exception:
        logger.debug("[FORCE_CANCEL] direct token fallback failed", exc_info=True)
    return None


def _safe_get_headers(context):
    """
    api_common.get_headers() に token_manager/global_state の待機・復元を任せる。

    以前の _has_api_token() は kabu_api.global_data を見ていたが、api_common 側は
    global_state.global_data を正として使っているため、API token refreshed 後も
    未準備扱いになることがあった。
    """
    global _LAST_TOKEN_WARN_AT
    try:
        return get_headers()
    except RuntimeError as e:
        if "API TOKEN is not set" in str(e):
            headers = _direct_token_fallback()
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
        headers = _direct_token_fallback()
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

    payload = {
        "OrderId": order_id,
        "Password": PASSWORD,
    }

    try:
        r = requests.put(
            f"{BASE_URL}/cancelorder",
            headers=headers,
            json=payload,
            timeout=(2, 5),
        )
        r.raise_for_status()

        logger.warning(
            f"[FORCE_CANCEL] order_id={order_id} "
            f"status={r.status_code} body={r.text}"
        )
        return True

    except HTTPError as e:
        logger.error(
            f"[FORCE_CANCEL] HTTP error order_id={order_id} "
            f"status={e.response.status_code if e.response else 'N/A'}"
        )
        return False

    except Exception:
        logger.exception(f"[FORCE_CANCEL] unexpected error order_id={order_id}")
        return False


# ============================================================
# kabu API : 注文取得
# ============================================================

def get_orders():
    headers = _safe_get_headers("get_orders")
    if headers is None:
        return []

    try:
        r = requests.get(
            f"{BASE_URL}/orders",
            headers=headers,          # ★ 認証必須
            timeout=(2, 5),
        )
        r.raise_for_status()

        data = r.json()

        # kabu API は dict / list 両方あり得る
        if isinstance(data, dict):
            return data.get("Orders", []) or data.get("orders", []) or []
        if isinstance(data, list):
            return data

        return []

    except HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            logger.error("❌ kabu API Unauthorized (401) in get_orders")
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


# ============================================================
# 強制キャンセルループ
# ============================================================

def start_force_cancel_loop(interval_sec=30):

    logger.warning("🛑 FORCE CANCEL LOOP START (%ss)", interval_sec)

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

                # 指値 & 未約定 & Cancel可能
                is_limit = price not in (0, None)
                is_open = qty and cum < qty
                can_cancel = state in CANCELABLE_STATES

                if is_limit and is_open and can_cancel:
                    logger.warning(
                        f"[FORCE_CANCEL] CANCEL "
                        f"order_id={order_id} state={state} "
                        f"{cum}/{qty}"
                    )
                    cancel_order(order_id)
                    time.sleep(0.3)  # API 連打防止

        except Exception:
            logger.exception("[FORCE_CANCEL LOOP ERROR]")

        time.sleep(interval_sec)
