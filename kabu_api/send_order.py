# ============================================================
# kabu_api/send_order.py（Ver23.9-FINAL-MARKET-GUARD）
# ------------------------------------------------------------
# ・成功時は dict {"OrderId": "...", "Price": <float>} を返す
# ・失敗時は None
# ・buy_sell_entry と entry_handler が完全に動作する形に統一
# ・レスポンスが文字列にならないように統一
#
# Ver23.9 重要修正:
# ・send_order_common 直前で市場時間ガードを必ず実行
# ・SUMMARY / TONOSAMA / RANKING のどの経路から来ても、昼休み・引け後はAPIへ送らない
# ・ログ上 11:34 に market closed skip が出ているのに sendorder 成功していたため、最終防衛線を追加
#
# 既存修正:
# ・制度信用固定運用を前提にする
# ・Code=100368 が出ても、ローカルで信用新規全体を60秒停止しない
# ・Code=100368 が出ても、SELL拒否キャッシュへ入れない
# ・次候補・次サイクルも必ずAPIへ送って、銘柄ごとの実エラーを確認する
# ・Code=100033 など銘柄個別制限だけ trade_restricted に入れる
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import configparser
import os
from typing import Any

import requests

from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

TRADE_RESTRICT_SEC = 1800
CREDIT_NEW_SUPPRESS_SEC = 0
CREDIT_NEW_SUPPRESS_KEY = "__CREDIT_NEW_ORDER_SUPPRESSED__"

_LAST_SEND_ORDER_ERROR: dict[str, Any] = {}

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


# ============================================================
# response helpers
# ============================================================

def _extract_code_message(data: Any) -> tuple[str, str]:
    code = ""
    message = ""
    try:
        if isinstance(data, dict):
            code = str(data.get("Code") or data.get("code") or "").strip()
            message = str(data.get("Message") or data.get("message") or "")
        else:
            message = str(data or "")
    except Exception:
        message = str(data or "")
    return code, message


def _safe_symbol(payload: dict) -> str:
    try:
        return str(payload.get("Symbol") or "").strip()
    except Exception:
        return ""


def _payload_side_name(payload: dict) -> str:
    try:
        side = int(payload.get("Side", 0))
        if side == 2:
            return "BUY"
        if side == 1:
            return "SELL"
        return str(payload.get("Side"))
    except Exception:
        return str(payload.get("Side"))


def _is_credit_new_order_payload(payload: dict) -> bool:
    try:
        return int(payload.get("CashMargin", 0)) == 2
    except Exception:
        return False


def _is_sell_credit_new_payload(payload: dict) -> bool:
    try:
        return int(payload.get("Side", 0)) == 1 and int(payload.get("CashMargin", 0)) == 2
    except Exception:
        return False


def _is_credit_new_suppressed(data: Any) -> bool:
    code, message = _extract_code_message(data)
    if code == "100368":
        return True
    if "信用新規" in message and "抑止" in message:
        return True
    return False


def _is_symbol_trade_restricted(data: Any) -> bool:
    code, message = _extract_code_message(data)
    if code == "100033":
        return True
    if "取引" in message and "制限" in message:
        return True
    return False


def _set_last_send_order_error(payload: dict, data: Any, *, status_code: Any = None) -> None:
    global _LAST_SEND_ORDER_ERROR
    code, message = _extract_code_message(data)
    _LAST_SEND_ORDER_ERROR = {
        "symbol": _safe_symbol(payload),
        "side": _payload_side_name(payload),
        "code": code,
        "message": message,
        "status_code": status_code,
        "cash_margin": payload.get("CashMargin"),
        "margin_trade_type": payload.get("MarginTradeType"),
        "account_type": payload.get("AccountType"),
        "front_order_type": payload.get("FrontOrderType"),
        "qty": payload.get("Qty"),
        "price": payload.get("Price"),
        "raw": data,
        "created_at": dt.datetime.now(),
    }


def clear_last_send_order_error() -> None:
    global _LAST_SEND_ORDER_ERROR
    _LAST_SEND_ORDER_ERROR = {}


def get_last_send_order_error() -> dict[str, Any]:
    try:
        return dict(_LAST_SEND_ORDER_ERROR)
    except Exception:
        return {}


# ============================================================
# market guard
# ============================================================

def _market_open_for_send_order() -> tuple[bool, str]:
    """
    sendorder API直前の最終防衛線。
    require_market_open=False の上位経路や、昼休み中に残った非同期処理から来ても止める。
    """
    if not _env_bool("SEND_ORDER_REQUIRE_MARKET_OPEN", True):
        return True, "disabled_by_env"
    try:
        from utils.business_day_utils import is_market_open
        ok = bool(is_market_open())
        return ok, "utils.business_day_utils.is_market_open"
    except Exception as e:
        # 判定不能時は安全側で止める。明示的に許可したい場合だけENVで解除する。
        if _env_bool("SEND_ORDER_FAIL_OPEN_IF_MARKET_CHECK_ERROR", False):
            return True, f"market_check_error_fail_open:{e}"
        return False, f"market_check_error:{e}"


def _block_if_market_closed(payload: dict) -> bool:
    ok, reason = _market_open_for_send_order()
    if ok:
        return False
    data = {
        "Code": "MARKET_CLOSED_LOCAL_GUARD",
        "Message": f"sendorder blocked because market is closed or market check failed: {reason}",
    }
    _set_last_send_order_error(payload, data, status_code="LOCAL")
    logger.warning(
        "⛔ SEND_ORDER_BLOCKED_MARKET_CLOSED symbol=%s side=%s qty=%s price=%s reason=%s",
        _safe_symbol(payload),
        _payload_side_name(payload),
        payload.get("Qty"),
        payload.get("Price"),
        reason,
    )
    return True


# ============================================================
# global_state helpers
# ============================================================

def _get_trade_restricted_root() -> dict:
    from global_state import global_data
    root = getattr(global_data, "trade_restricted", None)
    if not isinstance(root, dict):
        root = {}
        setattr(global_data, "trade_restricted", root)
    return root


def get_credit_new_order_suppressed_until() -> dt.datetime | None:
    return None


def is_credit_new_order_globally_suppressed() -> bool:
    return False


def _set_credit_new_suppressed(payload: dict, data: Any) -> None:
    try:
        if not _is_credit_new_order_payload(payload):
            return
        if not _is_credit_new_suppressed(data):
            return
        code, message = _extract_code_message(data)
        logger.warning(
            "🚫 CREDIT_NEW_ORDER_API_REJECT_NO_LOCAL_SUPPRESS symbol=%s side=%s code=%s margin_type=%s message=%s",
            _safe_symbol(payload),
            _payload_side_name(payload),
            code or "UNKNOWN",
            payload.get("MarginTradeType"),
            message,
        )
        logger.info(
            "⛔ ENTRY_SKIP %s reason=CREDIT_NEW_ORDER_API_REJECT detail=%s",
            _safe_symbol(payload) or "__UNKNOWN__",
            {
                "symbol": _safe_symbol(payload),
                "side": _payload_side_name(payload),
                "code": code or "UNKNOWN",
                "margin_trade_type": payload.get("MarginTradeType"),
                "message": message,
                "source": "kabu_api_100368_no_local_suppress",
            },
        )
    except Exception:
        logger.exception("[SEND ORDER] failed to log credit-new api reject payload=%s data=%s", payload, data)


def _clear_credit_new_suppressed_if_expired() -> None:
    return None


def _credit_new_suppressed_now(payload: dict) -> bool:
    return False


def _mark_symbol_trade_restricted_if_needed(payload: dict, data: Any) -> None:
    try:
        if not _is_credit_new_order_payload(payload):
            return
        if not _is_symbol_trade_restricted(data):
            return
        symbol = _safe_symbol(payload)
        if not symbol:
            return
        code, message = _extract_code_message(data)
        side = _payload_side_name(payload)
        until = dt.datetime.now() + dt.timedelta(seconds=TRADE_RESTRICT_SEC)
        root = _get_trade_restricted_root()
        root[symbol] = until
        logger.warning(
            "🚫 SYMBOL_TRADE_RESTRICTED_BY_KABU_API symbol=%s side=%s code=%s until=%s message=%s",
            symbol, side, code or "UNKNOWN", until, message,
        )
    except Exception:
        logger.exception("[SEND ORDER] failed to mark symbol trade_restricted payload=%s data=%s", payload, data)


def _mark_sell_reject_if_needed(payload: dict, data: Any) -> None:
    return None


def _handle_reject_response(payload: dict, data: Any, *, status_code: Any = None) -> None:
    _set_last_send_order_error(payload, data, status_code=status_code)
    _set_credit_new_suppressed(payload, data)
    _mark_sell_reject_if_needed(payload, data)
    _mark_symbol_trade_restricted_if_needed(payload, data)


def _log_send_attempt(payload: dict) -> None:
    try:
        logger.info(
            "[SEND ORDER ATTEMPT] symbol=%s side=%s cash_margin=%s margin_type=%s account_type=%s qty=%s price=%s front_order_type=%s",
            _safe_symbol(payload), _payload_side_name(payload), payload.get("CashMargin"),
            payload.get("MarginTradeType"), payload.get("AccountType"), payload.get("Qty"),
            payload.get("Price"), payload.get("FrontOrderType"),
        )
    except Exception:
        logger.debug("[SEND ORDER ATTEMPT] log failed payload=%s", payload, exc_info=True)


# ============================================================
# 🌐 統一注文 API（常に dict を返す）
# ============================================================

def send_order_common(payload: dict):
    if not isinstance(payload, dict):
        logger.error("❌ send_order_common: payload is not dict type=%s payload=%s", type(payload).__name__, payload)
        return None

    clear_last_send_order_error()

    # 最重要: 市場休憩・引け後は、どの経路から来ても送信しない。
    if _block_if_market_closed(payload):
        return None

    if _credit_new_suppressed_now(payload):
        _set_last_send_order_error(payload, {"Code": "LOCAL_SUPPRESS", "Message": "credit new local suppress"}, status_code="LOCAL")
        return None

    token = get_valid_token()
    if not token:
        _set_last_send_order_error(payload, {"Code": "TOKEN", "Message": "API token unavailable"}, status_code="LOCAL")
        logger.error("❌ send_order_common: APIトークン取得失敗")
        return None

    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    url = f"{API_URL}/sendorder"

    try:
        _log_send_attempt(payload)
        res = requests.post(url, json=payload, headers=headers, timeout=5)

        if res.status_code != 200:
            try:
                data = res.json()
            except Exception:
                data = res.text
            _handle_reject_response(payload, data, status_code=res.status_code)
            code, message = _extract_code_message(data)
            logger.error(
                "❌ HTTPエラー %s symbol=%s side=%s code=%s message=%s raw=%s",
                res.status_code, _safe_symbol(payload), _payload_side_name(payload), code, message, data,
            )
            return None

        try:
            data = res.json()
        except Exception:
            _set_last_send_order_error(payload, {"Code": "JSON", "Message": getattr(res, "text", "")}, status_code=getattr(res, "status_code", None))
            logger.error("❌ send_order_common: API JSON 解析失敗 text=%s", getattr(res, "text", ""))
            return None

        order_id = data.get("OrderId") if isinstance(data, dict) else None
        if not order_id:
            _handle_reject_response(payload, data, status_code=getattr(res, "status_code", None))
            code, message = _extract_code_message(data)
            logger.error(
                "❌ API応答異常（OrderIdなし） symbol=%s side=%s code=%s message=%s raw=%s",
                _safe_symbol(payload), _payload_side_name(payload), code, message, data,
            )
            return None

        try:
            executed_price = float(payload.get("Price", 0) or 0)
        except Exception:
            executed_price = 0.0

        logger.info("🟢 send_order_common 成功: symbol=%s side=%s OrderId=%s", _safe_symbol(payload), _payload_side_name(payload), order_id)
        return {"OrderId": order_id, "Price": executed_price}

    except requests.exceptions.Timeout:
        _set_last_send_order_error(payload, {"Code": "TIMEOUT", "Message": "sendorder timeout"}, status_code="LOCAL")
        logger.error("❌ send_order_common timeout symbol=%s side=%s", _safe_symbol(payload), _payload_side_name(payload), exc_info=True)
        return None

    except requests.exceptions.RequestException as e:
        _set_last_send_order_error(payload, {"Code": "REQUEST", "Message": str(e)}, status_code="LOCAL")
        logger.error("❌ send_order_common request exception symbol=%s side=%s err=%s", _safe_symbol(payload), _payload_side_name(payload), e, exc_info=True)
        return None

    except Exception as e:
        _set_last_send_order_error(payload, {"Code": "EXCEPTION", "Message": str(e)}, status_code="LOCAL")
        logger.error("❌ send_order_common 例外 symbol=%s side=%s err=%s", _safe_symbol(payload), _payload_side_name(payload), e, exc_info=True)
        return None
