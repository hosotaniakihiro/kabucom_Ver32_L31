# ============================================================
# kabu_api/send_order.py（Ver23.6-FINAL-CREDIT-NEW-SKIP-LOG）
# ------------------------------------------------------------
# ・成功時は dict {"OrderId": "...", "Price": <float>} を返す
# ・失敗時は None
# ・buy_sell_entry と entry_handler が完全に動作する形に統一
# ・レスポンスが文字列にならないように統一（最重要）
#
# 重要修正:
# ・kabu API が Code=100368 を返したら、信用新規全体を短時間停止する
#   - 100368: 現在、株式信用新規の注文は抑止されております。
#   - BUY/SELL どちらで出ても、kabu側が信用新規全体を拒否している状態として扱う
# ・停止中は /sendorder へ再送せずローカルで即スキップする
# ・停止中スキップ時は ENTRY_SKIP __GLOBAL__ reason=CREDIT_NEW_ORDER_SUPPRESSED を出す
#   - 「エントリーが発火しない」の理由をログで明確化する
# ・Code=100033 は銘柄個別 trade_restricted として扱う
# ・Code=100368 は銘柄個別 trade_restricted には入れない
# ・SELL 100368 は従来通り sell_order_reject_cache にも登録する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import configparser
from typing import Any

import requests

from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

# 銘柄別取引制限。Code=100033 用。
TRADE_RESTRICT_SEC = 1800

# 信用新規全体停止。Code=100368 用。
# 300秒だと長く「発火しない」ように見えやすいため、まず60秒で再確認する。
CREDIT_NEW_SUPPRESS_SEC = 60
CREDIT_NEW_SUPPRESS_KEY = "__CREDIT_NEW_ORDER_SUPPRESSED__"

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")


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
    """
    CashMargin=2 は信用新規。
    BUY/SELL とも信用新規注文として扱う。
    """
    try:
        return int(payload.get("CashMargin", 0)) == 2
    except Exception:
        return False


def _is_sell_credit_new_payload(payload: dict) -> bool:
    try:
        # kabu API: Side=1 が売り。CashMargin=2 が新規信用。
        return int(payload.get("Side", 0)) == 1 and int(payload.get("CashMargin", 0)) == 2
    except Exception:
        return False


def _is_credit_new_suppressed(data: Any) -> bool:
    """
    Code=100368:
      現在、株式信用新規の注文は抑止されております。
    """
    code, message = _extract_code_message(data)

    if code == "100368":
        return True

    if "信用新規" in message and "抑止" in message:
        return True

    return False


def _is_symbol_trade_restricted(data: Any) -> bool:
    """
    Code=100033 など、銘柄・取引に対する個別制限。
    100368 はここに入れない。
    """
    code, message = _extract_code_message(data)

    if code == "100033":
        return True

    if "取引" in message and "制限" in message:
        return True

    return False


# ============================================================
# global_state helpers
# ============================================================

def _get_trade_restricted_root() -> dict:
    """
    global_data.trade_restricted を必ず dict として取得する。
    """
    from global_state import global_data

    root = getattr(global_data, "trade_restricted", None)
    if not isinstance(root, dict):
        root = {}
        setattr(global_data, "trade_restricted", root)

    return root


def get_credit_new_order_suppressed_until() -> dt.datetime | None:
    """
    信用新規注文全体の停止期限を取得する。

    entry_controller など上位レイヤからも参照できるよう public 名にしている。
    """
    try:
        from global_state import global_data

        until = getattr(global_data, "credit_new_order_suppressed_until", None)
        if isinstance(until, dt.datetime):
            return until

        root = _get_trade_restricted_root()
        until = root.get(CREDIT_NEW_SUPPRESS_KEY)
        if isinstance(until, dt.datetime):
            return until

        return None

    except Exception:
        logger.exception("[SEND ORDER] credit-new suppress state check failed")
        return None


def is_credit_new_order_globally_suppressed() -> bool:
    """
    信用新規注文全体が現在停止中かどうか。
    """
    try:
        _clear_credit_new_suppressed_if_expired()

        until = get_credit_new_order_suppressed_until()
        if not isinstance(until, dt.datetime):
            return False

        return dt.datetime.now() < until

    except Exception:
        logger.exception("[SEND ORDER] credit-new suppress public check failed")
        return False


def _set_credit_new_suppressed(payload: dict, data: Any) -> None:
    """
    Code=100368 を検出したら、銘柄単位ではなく信用新規注文全体を一時停止する。
    """
    try:
        if not _is_credit_new_order_payload(payload):
            return

        if not _is_credit_new_suppressed(data):
            return

        symbol = _safe_symbol(payload)
        side = _payload_side_name(payload)
        code, message = _extract_code_message(data)

        until = dt.datetime.now() + dt.timedelta(seconds=CREDIT_NEW_SUPPRESS_SEC)

        from global_state import global_data

        setattr(global_data, "credit_new_order_suppressed_until", until)

        root = _get_trade_restricted_root()
        root[CREDIT_NEW_SUPPRESS_KEY] = until

        logger.warning(
            "🚫 CREDIT_NEW_ORDER_GLOBAL_SUPPRESSED symbol=%s side=%s code=%s until=%s sec=%s message=%s",
            symbol,
            side,
            code or "UNKNOWN",
            until,
            CREDIT_NEW_SUPPRESS_SEC,
            message,
        )

        logger.info(
            "⛔ ENTRY_SKIP __GLOBAL__ reason=CREDIT_NEW_ORDER_SUPPRESSED detail=%s",
            {
                "symbol": symbol,
                "side": side,
                "code": code or "UNKNOWN",
                "until": str(until),
                "sec": CREDIT_NEW_SUPPRESS_SEC,
                "message": message,
                "source": "kabu_api_100368",
            },
        )

    except Exception:
        logger.exception(
            "[SEND ORDER] failed to set credit-new global suppress payload=%s data=%s",
            payload,
            data,
        )


def _clear_credit_new_suppressed_if_expired() -> None:
    """
    信用新規注文全体停止が期限切れなら解除する。
    """
    try:
        from global_state import global_data

        until = get_credit_new_order_suppressed_until()
        if not isinstance(until, dt.datetime):
            return

        if dt.datetime.now() < until:
            return

        if getattr(global_data, "credit_new_order_suppressed_until", None) == until:
            setattr(global_data, "credit_new_order_suppressed_until", None)

        root = _get_trade_restricted_root()
        if root.get(CREDIT_NEW_SUPPRESS_KEY) == until:
            root.pop(CREDIT_NEW_SUPPRESS_KEY, None)

        logger.warning(
            "🟢 CREDIT_NEW_ORDER_SUPPRESSED_LOCAL_RELEASED until=%s",
            until,
        )

    except Exception:
        logger.exception("[SEND ORDER] failed to clear expired credit-new suppress")


def _credit_new_suppressed_now(payload: dict) -> bool:
    """
    100368 発生後、一定時間は信用新規注文をAPIに送らない。
    BUY/SELL 共通。
    """
    try:
        if not _is_credit_new_order_payload(payload):
            return False

        _clear_credit_new_suppressed_if_expired()

        until = get_credit_new_order_suppressed_until()
        if not isinstance(until, dt.datetime):
            return False

        now = dt.datetime.now()
        if now < until:
            remain = max(0.0, (until - now).total_seconds())
            symbol = _safe_symbol(payload)
            side = _payload_side_name(payload)

            logger.warning(
                "🚫 CREDIT_NEW_ORDER_SUPPRESSED_LOCAL_SKIP symbol=%s side=%s until=%s remain=%.1fs",
                symbol,
                side,
                until,
                remain,
            )

            # entry_controller まで到達していないように見える問題を避けるため、
            # send_order 側でも ENTRY_SKIP 形式で理由を出す。
            logger.info(
                "⛔ ENTRY_SKIP __GLOBAL__ reason=CREDIT_NEW_ORDER_SUPPRESSED detail=%s",
                {
                    "symbol": symbol,
                    "side": side,
                    "until": str(until),
                    "remain_sec": round(remain, 1),
                    "source": "kabu_api_local_cooldown",
                },
            )
            return True

        return False

    except Exception:
        logger.exception(
            "[SEND ORDER] credit-new local suppress precheck failed payload=%s",
            payload,
        )
        return False


def _mark_symbol_trade_restricted_if_needed(payload: dict, data: Any) -> None:
    """
    Code=100033 等の銘柄個別の取引制限だけを symbol trade_restricted に入れる。

    重要:
      Code=100368 は信用新規全体の抑止なので、symbol個別停止にはしない。
    """
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
            symbol,
            side,
            code or "UNKNOWN",
            until,
            message,
        )

    except Exception:
        logger.exception(
            "[SEND ORDER] failed to mark symbol trade_restricted payload=%s data=%s",
            payload,
            data,
        )


def _mark_sell_reject_if_needed(payload: dict, data: Any) -> None:
    """
    SELL信用新規で100368が返った場合は、SELL拒否キャッシュにも入れる。
    ただし100368自体は信用新規全体抑止としても扱う。
    """
    try:
        if not _is_sell_credit_new_payload(payload):
            return

        code, message = _extract_code_message(data)

        if code != "100368" and not ("信用新規" in message and "抑止" in message):
            return

        symbol = _safe_symbol(payload)
        if not symbol:
            return

        from AI.sell_order_reject_cache import mark_sell_rejected

        mark_sell_rejected(
            symbol,
            code=code or "100368",
            message=message,
            source="kabu_api.send_order_common",
        )

    except Exception:
        logger.exception(
            "[SEND ORDER] failed to mark sell reject cache payload=%s data=%s",
            payload,
            data,
        )


def _handle_reject_response(payload: dict, data: Any) -> None:
    """
    kabu API の拒否応答を分類して記録する。

    100368:
      信用新規注文全体の抑止。
      → global credit-new suppress に入れる。

    100033:
      銘柄・取引制限。
      → symbol trade_restricted に入れる。
    """
    _set_credit_new_suppressed(payload, data)
    _mark_sell_reject_if_needed(payload, data)
    _mark_symbol_trade_restricted_if_needed(payload, data)


def _log_send_attempt(payload: dict) -> None:
    try:
        logger.info(
            "[SEND ORDER ATTEMPT] symbol=%s side=%s cash_margin=%s qty=%s price=%s front_order_type=%s",
            _safe_symbol(payload),
            _payload_side_name(payload),
            payload.get("CashMargin"),
            payload.get("Qty"),
            payload.get("Price"),
            payload.get("FrontOrderType"),
        )
    except Exception:
        logger.debug("[SEND ORDER ATTEMPT] log failed payload=%s", payload, exc_info=True)


# ============================================================
# 🌐 統一注文 API（常に dict を返す）
# ============================================================

def send_order_common(payload: dict):
    """
    kabuステーションAPI /sendorder を呼ぶ共通関数。

    戻り値（成功時）:
        { "OrderId": "...", "Price": float }

    戻り値（失敗時）:
        None
    """

    if not isinstance(payload, dict):
        logger.error(
            "❌ send_order_common: payload is not dict type=%s payload=%s",
            type(payload).__name__,
            payload,
        )
        return None

    # 100368 発生中は信用新規注文をAPIに送らない。
    if _credit_new_suppressed_now(payload):
        return None

    token = get_valid_token()
    if not token:
        logger.error("❌ send_order_common: APIトークン取得失敗")
        return None

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token,
    }

    url = f"{API_URL}/sendorder"

    try:
        _log_send_attempt(payload)

        res = requests.post(url, json=payload, headers=headers, timeout=5)

        # -------------------------------------------------------
        # HTTPエラー処理（JSONを取り出してログに表示）
        # -------------------------------------------------------
        if res.status_code != 200:
            try:
                data = res.json()
            except Exception:
                data = res.text

            _handle_reject_response(payload, data)

            code, message = _extract_code_message(data)
            logger.error(
                "❌ HTTPエラー %s symbol=%s side=%s code=%s message=%s raw=%s",
                res.status_code,
                _safe_symbol(payload),
                _payload_side_name(payload),
                code,
                message,
                data,
            )
            return None

        # -------------------------------------------------------
        # レスポンスJSON
        # -------------------------------------------------------
        try:
            data = res.json()
        except Exception:
            logger.error(
                "❌ send_order_common: API JSON 解析失敗 text=%s",
                getattr(res, "text", ""),
            )
            return None

        order_id = data.get("OrderId") if isinstance(data, dict) else None
        if not order_id:
            _handle_reject_response(payload, data)

            code, message = _extract_code_message(data)
            logger.error(
                "❌ API応答異常（OrderIdなし） symbol=%s side=%s code=%s message=%s raw=%s",
                _safe_symbol(payload),
                _payload_side_name(payload),
                code,
                message,
                data,
            )
            return None

        # kabuS API は約定価格を返さないため payload.Price を返す
        try:
            executed_price = float(payload.get("Price", 0) or 0)
        except Exception:
            executed_price = 0.0

        logger.info(
            "🟢 send_order_common 成功: symbol=%s side=%s OrderId=%s",
            _safe_symbol(payload),
            _payload_side_name(payload),
            order_id,
        )

        # ★★★ 最重要：dict で返す（文字列だけ返さない！）★★★
        return {
            "OrderId": order_id,
            "Price": executed_price,
        }

    except requests.exceptions.Timeout:
        logger.error(
            "❌ send_order_common timeout symbol=%s side=%s",
            _safe_symbol(payload),
            _payload_side_name(payload),
            exc_info=True,
        )
        return None

    except requests.exceptions.RequestException as e:
        logger.error(
            "❌ send_order_common request exception symbol=%s side=%s err=%s",
            _safe_symbol(payload),
            _payload_side_name(payload),
            e,
            exc_info=True,
        )
        return None

    except Exception as e:
        logger.error(
            "❌ send_order_common 例外 symbol=%s side=%s err=%s",
            _safe_symbol(payload),
            _payload_side_name(payload),
            e,
            exc_info=True,
        )
        return None
