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

import contextlib as _contextlib
import sys as _sys
import contextvars as _contextvars

# 2026-10-19: 新規注文の信用区分を呼び出し側で決める (1=制度、3=一般デイトレ)。
#   with margin_trade_type_override(1): place_entry_sell(...) のように使う。スレッド/コンテキストごと。
_FORCED_MARGIN_TRADE_TYPE: "_contextvars.ContextVar[int | None]" = _contextvars.ContextVar("forced_margin_trade_type", default=None)


@_contextlib.contextmanager
def margin_trade_type_override(margin_trade_type: int):
    token = _FORCED_MARGIN_TRADE_TYPE.set(int(margin_trade_type))
    try:
        yield
    finally:
        _FORCED_MARGIN_TRADE_TYPE.reset(token)

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


def _margin_type_reject_enabled() -> bool:
    """MarginTradeType 不正を銘柄固有の制限として扱うか (2026-09-25)。"""
    import os

    v = str(os.getenv("ENTRY_MARK_MARGIN_TYPE_REJECT", "1")).strip().lower()
    return v not in ("0", "false", "no", "off")


def _margin_type_restrict_sec() -> int:
    """MarginTradeType 不正で止めておく秒数。既定 21600 (6 時間 = 当日いっぱい)。

    100033 の一時的な取引制限 (TRADE_RESTRICT_SEC=1800) と違い、
    日計り信用の対象外かどうかは当日中変わらないので長く止める。
    """
    import os

    try:
        return max(60, int(float(os.getenv("MARGIN_TYPE_RESTRICT_SEC", "21600") or 21600)))
    except Exception:
        return 21600


def _is_margin_type_unsupported(data: Any) -> bool:
    """日計り信用などの信用区分が使えない銘柄か (2026-09-25)。

    9/25 の 4875 は margin_type=3 (日計り信用) で
      Code=4002013 パラメータ不正：MarginTradeType
    が返った。同じ margin_type=3 で 6904 は約定しているので設定側の誤りでは
    なく、銘柄がデイトレ信用の対象外という固有の性質。
    reason=ORDER_ID_EMPTY_RETRYABLE として再試行されるのを止める。
    """
    if not _margin_type_reject_enabled():
        return False
    code, message = _extract_code_message(data)
    if code != "4002013":
        return False
    return "margintradetype" in str(message or "").lower()


def _margin_type_fallback_enabled() -> bool:
    """日計り信用が使えない銘柄を制度信用で建て直すか (2026-09-29)。既定 ON。

    9/25 の 4875、9/29 の新規上場株群 (627A / 625A / 634A / 646A / 593A /
    621A / 604A ...) は margin_type=3 で Code=4002013 MarginTradeType が返る。
    これらは値上がり率上位に頻出するので、6 時間止めると当日いっぱい触れない。
    制度信用 (MarginTradeType=1) なら建てられる。

    持ち越しの安全性は確認済み: DAYTRADE_FORCE_CLOSE_ONLY_MARGIN_TYPE="0" なので
    15:24:30 の強制決済は MarginTradeType=1 も対象にする (kabu_api/daytrade_credit.py
    _is_target_position)。新規は ENTRY_NO_NEW_AFTER=15:00 で止まる。

    注意: 日計り信用は証券会社側も当日中に強制返済するが、制度信用は 6 か月
    なので**自前の強制決済だけが持ち越しを防ぐ**。停止時はここが効かない。

    ENTRY_MARGIN_TYPE_FALLBACK_ENABLED=0 で従来動作 (6 時間ブロック) に戻る。
    """
    import os

    v = str(os.getenv("ENTRY_MARGIN_TYPE_FALLBACK_ENABLED", "1")).strip().lower()
    return v not in ("0", "false", "no", "off", "ng", "disabled")


def _margin_type_fallback_value() -> int:
    """フォールバック先の信用区分。既定 1 (制度信用)。"""
    import os

    try:
        return int(float(os.getenv("ENTRY_MARGIN_TYPE_FALLBACK", "1") or 1))
    except Exception:
        return 1


def _margin_type_fallback_applicable(payload: dict, data: Any) -> bool:
    """この拒否を制度信用で再送してよいか。"""
    if not _margin_type_fallback_enabled():
        return False
    if not _is_margin_type_unsupported(data):
        return False
    try:
        if int(float(payload.get("CashMargin") or 0)) != 2:
            return False  # 新規の信用建て以外は触らない
        cur = int(float(payload.get("MarginTradeType") or 0))
    except Exception:
        return False
    nxt = _margin_type_fallback_value()
    if nxt <= 0 or nxt == cur:
        return False
    return True


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
        # 2026-09-25: 100033 (一時的な取引制限) と 4002013 (MarginTradeType 不正)
        #   の両方を拾う。後者は日計り信用の対象外という銘柄固有の性質なので
        #   当日いっぱい止める。
        _margin_ng = _is_margin_type_unsupported(data)
        if not _margin_ng and not _is_symbol_trade_restricted(data):
            return
        symbol = _safe_symbol(payload)
        if not symbol:
            return
        code, message = _extract_code_message(data)
        side = _payload_side_name(payload)
        _sec = _margin_type_restrict_sec() if _margin_ng else TRADE_RESTRICT_SEC
        until = dt.datetime.now() + dt.timedelta(seconds=_sec)
        root = _get_trade_restricted_root()
        root[symbol] = until
        logger.warning(
            "🚫 SYMBOL_TRADE_RESTRICTED_BY_KABU_API symbol=%s side=%s code=%s until=%s sec=%d "
            "kind=%s message=%s",
            symbol, side, code or "UNKNOWN", until, _sec,
            "margin_type_unsupported" if _margin_ng else "trade_restricted",
            message,
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

def _retry_with_fallback_margin(payload: dict, data: Any, *, status_code: Any = None):
    """日計り信用が使えない銘柄を制度信用で 1 回だけ建て直す。"""
    nxt = _margin_type_fallback_value()
    code, message = _extract_code_message(data)
    new_payload = dict(payload)
    new_payload["MarginTradeType"] = nxt
    logger.warning(
        "[SEND ORDER MARGIN FALLBACK] symbol=%s side=%s margin_type=%s -> %s "
        "code=%s message=%s status=%s (日計り信用の対象外なので制度信用で再送。"
        "15:24:30 の強制決済は MarginTradeType=%s も対象)",
        _safe_symbol(payload), _payload_side_name(payload),
        payload.get("MarginTradeType"), nxt, code, message, status_code, nxt,
    )
    return send_order_common(new_payload, _margin_fallback_done=True)


# ============================================================
# 2026-10-01: 新規エントリーの停止スイッチ (再起動不要)
#   %LOCALAPPDATA%\autostock\runtime\ENTRY_PAUSE があれば新規注文
#   (信用新規 CashMargin=2 / 現物買い) を送らない。返済は止めない。
#   ファイルを消せば即再開。発注ごとに存在確認するだけ。
# ============================================================
def _entry_pause_file():
    import os
    from pathlib import Path

    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "autostock" / "runtime" / "ENTRY_PAUSE"


def _entry_paused_by_file(payload: dict) -> bool:
    try:
        cm = int(payload.get("CashMargin") or 0)
        is_entry = cm == 2 or (cm == 1 and str(payload.get("Side")) == "2")
        if not is_entry or not _entry_pause_file().exists():
            return False
        logger.warning(
            "[ENTRY PAUSE] 新規注文を送らない (停止ファイルあり) symbol=%s side=%s qty=%s file=%s",
            payload.get("Symbol"), payload.get("Side"), payload.get("Qty"), _entry_pause_file(),
        )
        return True
    except Exception:
        return False


_PREMIUM_CACHE: dict = {}  # symbol -> (取得時刻, DayTrade dict)

# 2026-10-20: 戦略ごとのプレミアム料上限 (%)。SURGE SHORT は 1 回 +2% 前後を取るので 0.4% まで払う (ユーザー判断)。
_PREMIUM_CAP_PCT = _contextvars.ContextVar("daytrade_short_premium_cap_pct", default=None)


@_contextlib.contextmanager
def premium_cap_override(max_pct: float):
    """with の中で出す新規売りだけ、DAYTRADE_SHORT_MAX_PREMIUM_PCT の代わりに max_pct を上限にする。"""
    tok = _PREMIUM_CAP_PCT.set(float(max_pct))
    try:
        yield
    finally:
        _PREMIUM_CAP_PCT.reset(tok)


def _daytrade_premium(symbol: str):
    """/margin/marginpremium の DayTrade 部分 (円/株)。5 分キャッシュ。取れなければ None。"""
    now = dt.datetime.now().timestamp()
    hit = _PREMIUM_CACHE.get(symbol)
    if hit and now - hit[0] < 300:
        return hit[1]
    token = get_valid_token()
    if not token:
        return None
    try:
        r = requests.get(f"{API_URL}/margin/marginpremium/{symbol}", headers={"X-API-KEY": token}, timeout=3)
        if r.status_code != 200:
            logger.warning("[DAYTRADE SHORT PREMIUM] marginpremium status=%s symbol=%s body=%s", r.status_code, symbol, r.text[:200])
            return None
        d = (r.json() or {}).get("DayTrade") or {}
    except Exception:
        logger.warning("[DAYTRADE SHORT PREMIUM] marginpremium failed symbol=%s err=%s", symbol, _sys.exc_info()[1])
        return None
    _PREMIUM_CACHE[symbol] = (now, d)
    return d


def _daytrade_short_premium_block_reason(payload: dict) -> str:
    """一般信用デイトレ (MarginTradeType=3) の新規売りで、プレミアム料が高すぎれば理由を返す。それ以外は ""。

    プレミアム料が取れないときも送らない (4493 のような 0.5% を見落とすより、1 回見送るほうが安い)。
    """
    try:
        if not _is_sell_credit_new_payload(payload) or int(payload.get("MarginTradeType") or 0) != 3:
            return ""
        _cap = _PREMIUM_CAP_PCT.get()
        max_pct = float(_cap) if _cap is not None else float(os.getenv("DAYTRADE_SHORT_MAX_PREMIUM_PCT", "0.1"))
        if max_pct < 0:
            return ""
        sym = _safe_symbol(payload)
        d = _daytrade_premium(sym)
        if d is None:
            if _cap is not None:
                return ""  # 上限を渡した戦略 (SURGE SHORT) は発注直前に自分で照会済み。kabu Station が遅いだけで止めない
            return "reason=premium_unavailable"
        if d.get("MarginPremiumType") is None:
            return ""  # デイトレ空売りの対象外。証券会社が断り、制度信用への切替 (_retry_with_fallback_margin) に任せる
        prem = float(d.get("MarginPremium") or 0.0)
        if prem <= 0:
            return ""
        price = float(payload.get("Price") or 0.0)
        if price <= 0:
            return f"reason=premium_no_price premium_yen={prem}"
        pct = prem / price * 100.0
        if pct > max_pct:
            return f"reason=premium_too_high premium_yen={prem} premium_pct={pct:.3f} max_pct={max_pct}"
        return ""
    except Exception:
        logger.exception("[DAYTRADE SHORT PREMIUM] check failed")
        return "reason=premium_check_error"


def send_order_common(payload: dict, *, _margin_fallback_done: bool = False):
    if not isinstance(payload, dict):
        logger.error("❌ send_order_common: payload is not dict type=%s payload=%s", type(payload).__name__, payload)
        return None

    clear_last_send_order_error()

    # 最重要: 市場休憩・引け後は、どの経路から来ても送信しない。
    if _block_if_market_closed(payload):
        return None

    # 2026-10-19: 信用の新規買いを止める (ユーザー判断)。ENTRY_BUY_ENABLED=1 で戻す。
    #   過去 18 日の実売買で 買い 215 回 -62,140 円 / 売り 32 回 +45,240 円。返済 (CashMargin=3) は止めない。
    if (_is_credit_new_order_payload(payload) and str(payload.get("Side")) == "2"
            and not _env_bool("ENTRY_BUY_ENABLED", False)):
        logger.warning("[ENTRY BUY DISABLED] 新規買いを送らない symbol=%s qty=%s price=%s (ENTRY_BUY_ENABLED=1 で戻す)",
                       _safe_symbol(payload), payload.get("Qty"), payload.get("Price"))
        _set_last_send_order_error(payload, {"Code": "ENTRY_BUY_DISABLED", "Message": "credit new buy disabled"}, status_code="LOCAL")
        return None

    # 2026-10-19: 呼び出し側が信用区分を指定した新規 (OVEREXT_SHORT の制度信用など)。margin_trade_type_override 参照。
    try:
        _forced = _FORCED_MARGIN_TRADE_TYPE.get()
        if _forced and _is_credit_new_order_payload(payload) and int(payload.get("MarginTradeType") or 0) != int(_forced):
            logger.warning("[MARGIN TYPE OVERRIDE] symbol=%s side=%s MarginTradeType %s -> %s",
                           _safe_symbol(payload), payload.get("Side"), payload.get("MarginTradeType"), _forced)
            payload["MarginTradeType"] = int(_forced)
    except Exception:
        logger.exception("[MARGIN TYPE OVERRIDE] failed")

    # 2026-10-19: ENTRY_NO_NEW_AFTER (15:00) 以降の信用新規は制度信用 (MarginTradeType=1) で出す (ユーザー判断)。
    #   一般信用デイトレ (3) は日計りで当日中に返済が必要なため。制度の建玉は 15:24:30 の強制決済の対象外で持ち越しになる。
    try:
        if (_is_credit_new_order_payload(payload) and int(payload.get("MarginTradeType") or 0) != 1
                and _env_bool("ENTRY_AFTER_CUTOFF_SEIDO_ENABLED", True)):
            hh, mm = [int(x) for x in str(os.getenv("ENTRY_NO_NEW_AFTER", "15:00")).split(":")[:2]]
            if dt.datetime.now().time() >= dt.time(hh, mm):
                logger.warning("[ENTRY SEIDO AFTER CUTOFF] symbol=%s side=%s MarginTradeType %s -> 1 (制度信用、持ち越し)",
                               _safe_symbol(payload), payload.get("Side"), payload.get("MarginTradeType"))
                payload["MarginTradeType"] = 1
                # 15:24:30 の強制決済で返済されないよう持ち越し登録する (制度は全区分が強制決済の対象)
                try:
                    from trading.strategies.overnight_holds import register as _register_overnight
                    _register_overnight(_safe_symbol(payload), "ENTRY_AFTER_CUTOFF_SEIDO", days=3)
                except Exception:
                    logger.exception("[ENTRY SEIDO AFTER CUTOFF] overnight register failed")
    except Exception:
        logger.exception("[ENTRY SEIDO AFTER CUTOFF] margin type rewrite failed")

    # 2026-10-20: 一般信用デイトレの新規売りは、プレミアム料が売値の DAYTRADE_SHORT_MAX_PREMIUM_PCT (0.1%) を超えたら送らない。
    #   10/20 の 4493 は 100 株 1,680 円 (売値の 0.5%) を 2 回払った。研究で見えた有利さは多くて 0.1%。
    _premium_reason = _daytrade_short_premium_block_reason(payload)
    if _premium_reason:
        logger.warning("[DAYTRADE SHORT PREMIUM] 新規売りを送らない symbol=%s price=%s %s",
                       _safe_symbol(payload), payload.get("Price"), _premium_reason)
        _set_last_send_order_error(payload, {"Code": "DAYTRADE_SHORT_PREMIUM", "Message": _premium_reason}, status_code="LOCAL")
        return None

    if _credit_new_suppressed_now(payload):
        _set_last_send_order_error(payload, {"Code": "LOCAL_SUPPRESS", "Message": "credit new local suppress"}, status_code="LOCAL")
        return None

    if _entry_paused_by_file(payload):
        _set_last_send_order_error(payload, {"Code": "ENTRY_PAUSE", "Message": "entry paused by file"}, status_code="LOCAL")
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
            if not _margin_fallback_done and _margin_type_fallback_applicable(payload, data):
                return _retry_with_fallback_margin(payload, data, status_code=res.status_code)
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
            if not _margin_fallback_done and _margin_type_fallback_applicable(payload, data):
                return _retry_with_fallback_margin(
                    payload, data, status_code=getattr(res, "status_code", None))
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
