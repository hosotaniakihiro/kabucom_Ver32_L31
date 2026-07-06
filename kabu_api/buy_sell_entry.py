# ============================================================
# File   : kabu_api/buy_sell_entry.py
# Version: Ver28-PRODUCTION-SOR-EXCHANGE9
# ------------------------------------------------------------
# ✔ Ver27 完全互換ベース
# ✔ 機能削除ゼロ
# ✔ 上位レイヤ計算 qty を優先使用
# ✔ qty=None のときのみ固定100株互換
# ✔ 70万円ワンショット最終防衛
# ✔ payload 数値型完全保証
# ✔ price / stop_price 型安全化
# ✔ API戻り値安全化
# ✔ send_order_common 完全診断ログ
# ✔ kabu API レスポンス完全可視化
# ✔ None発生原因完全特定
# ✔ MARKET注文で板が取れない場合も reference_price があれば数量防衛して発注継続
# ✔ Ver28: 新規エントリー注文の Exchange を既定で SOR=9 に統一
# ============================================================

import logging
import configparser
import os
from typing import Optional

from utils_common import (
    get_latest_bid_ask,
    get_trading_unit,
)

from kabu_api.send_order import send_order_common

logger = logging.getLogger(__name__)

# ============================================================
# 固定ルール
# ============================================================

DEFAULT_FALLBACK_QTY = 100
MAX_ONESHOT = 700_000

# kabuステーションAPIのSOR指定は Exchange=9。
# ユーザー方針: 新規エントリーはSORへ統一。
ENTRY_EXCHANGE = int(float(os.getenv("ENTRY_EXCHANGE", os.getenv("KABU_ENTRY_EXCHANGE", "9"))))
FORCE_ENTRY_SOR = str(os.getenv("ENTRY_FORCE_SOR", "1")).strip().lower() not in {"0", "false", "no", "off", "disable", "disabled"}

# ============================================================
# settings.ini
# ============================================================

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")


# ============================================================
# helper
# ============================================================

def _safe_int(v, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _resolve_entry_exchange(exchange=None) -> int:
    """Resolve exchange for new entry orders.

    FORCE_ENTRY_SOR=1 makes all entry payloads use Exchange=9.
    This does not change exit/cancel code paths outside this module.
    """
    try:
        if FORCE_ENTRY_SOR:
            return 9
        x = _safe_int(exchange, 0)
        if x > 0:
            return x
        return _safe_int(ENTRY_EXCHANGE, 9) or 9
    except Exception:
        return 9


def _resolve_requested_qty(qty) -> int:
    """
    上位レイヤから渡された数量を優先する。
    無効な場合のみ fallback する。
    """
    q = _safe_int(qty, 0)
    if q > 0:
        return q
    return DEFAULT_FALLBACK_QTY


def _normalize_qty_to_unit(symbol, qty: int) -> int:
    """
    売買単位に丸める。
    """
    try:
        unit = get_trading_unit(symbol)
        unit = _safe_int(unit, 0)

        logger.info(
            "[QTY UNIT CHECK] symbol=%s requested_qty=%s trading_unit=%s",
            symbol,
            qty,
            unit,
        )

        if unit <= 0:
            logger.warning(
                "⚠ trading_unit 不明のため数量をそのまま使用 symbol=%s qty=%s",
                symbol,
                qty,
            )
            return max(0, _safe_int(qty, 0))

        q = _safe_int(qty, 0)
        if q <= 0:
            return 0

        normalized = (q // unit) * unit

        if normalized <= 0:
            logger.warning(
                "⚠ 売買単位未満のため発注不可 symbol=%s requested_qty=%s trading_unit=%s",
                symbol,
                q,
                unit,
            )
            return 0

        return normalized

    except Exception:
        logger.error("❌ _normalize_qty_to_unit エラー", exc_info=True)
        return 0


def _resolve_market_reference_price(symbol, quotes, reference_price, *, side: str) -> float:
    """
    MARKET注文の数量防衛・70万円制限用の参考価格を解決する。

    成行payload自体は Price=0 のまま送るが、数量計算には価格が必要。
    板が取得できる場合は BUY=ask / SELL=bid を優先し、
    板が無い場合は上位レイヤが渡した summary close 等の reference_price を使う。
    """
    try:
        side_u = str(side or "").upper()
        q_price = 0.0

        if isinstance(quotes, dict):
            if side_u == "BUY":
                q_price = _safe_float(quotes.get("ask_price"), 0.0)
            else:
                q_price = _safe_float(quotes.get("bid_price"), 0.0)

        if q_price > 0:
            return q_price

        ref = _safe_float(reference_price, 0.0)
        if ref > 0:
            logger.warning(
                "⚠ %s: MARKET %s 板価格なし -> reference_price 使用 price=%s",
                symbol,
                side_u,
                ref,
            )
            return ref

        logger.warning(
            "⚠ %s: MARKET %s 参考価格なし quotes=%s reference_price=%s",
            symbol,
            side_u,
            quotes,
            reference_price,
        )
        return 0.0

    except Exception:
        logger.error("❌ _resolve_market_reference_price エラー symbol=%s", symbol, exc_info=True)
        return 0.0


# ============================================================
# ワンショット制限
# ============================================================

def _enforce_oneshot_rule(symbol, price, qty):
    """
    指定 qty を使いつつ、70万円ワンショット制限を守る。
    """
    try:
        if price is None:
            logger.warning("⚠ %s price=None", symbol)
            return 0

        price = float(price)
        qty = _safe_int(qty, 0)

        if price <= 0:
            logger.warning("⚠ %s price<=0 %s", symbol, price)
            return 0

        if qty <= 0:
            logger.warning("⚠ %s qty<=0 %s", symbol, qty)
            return 0

        total = price * qty

        if total > MAX_ONESHOT:
            logger.warning(
                "🚫 ONESHOT制限超過 symbol=%s price=%s qty=%s total=%s max_oneshot=%s",
                symbol,
                price,
                qty,
                int(total),
                int(MAX_ONESHOT),
            )
            return 0

        return qty

    except Exception:
        logger.error("❌ _enforce_oneshot_rule エラー", exc_info=True)
        return 0


# ============================================================
# Payload Builder
# ============================================================

def _make_payload(
    symbol,
    side,
    qty,
    price,
    *,
    exchange=None,
    margin_type=1,
    cash_margin=2,
    front_order_type=20,
    stop_price=None,
):
    qty = int(qty)
    resolved_exchange = _resolve_entry_exchange(exchange)

    payload = {
        "Password": Password,
        "Symbol": str(symbol),
        "Exchange": int(resolved_exchange),
        "SecurityType": 1,
        "Side": int(side),
        "CashMargin": int(cash_margin),
        "MarginTradeType": int(margin_type),
        "DelivType": 0,
        "AccountType": 4,
        "Qty": qty,
        "FrontOrderType": int(front_order_type),
        "ExpireDay": 0,
    }

    if front_order_type in (10, 20):
        payload["Price"] = float(price) if price is not None else 0.0

    if front_order_type == 30:
        payload["ReverseLimitOrder"] = 1
        payload["StopPrice"] = int(float(stop_price))
        payload["Price"] = 0

    logger.warning(
        "[ENTRY ORDER PAYLOAD BUILD] symbol=%s side=%s qty=%s price=%s front_order_type=%s exchange=%s sor=%s force_sor=%s",
        symbol,
        side,
        qty,
        payload.get("Price"),
        front_order_type,
        payload.get("Exchange"),
        payload.get("Exchange") == 9,
        FORCE_ENTRY_SOR,
    )
    return payload


# ============================================================
# API SEND WRAPPER
# ============================================================

def _send_order(payload, symbol):
    try:
        logger.info(
            "[KABU PAYLOAD] symbol=%s exchange=%s sor=%s payload=%s",
            symbol,
            payload.get("Exchange") if isinstance(payload, dict) else None,
            bool(isinstance(payload, dict) and payload.get("Exchange") == 9),
            payload,
        )

        res = send_order_common(payload)

        logger.info(
            "[KABU RESPONSE RAW] symbol=%s res=%s",
            symbol,
            res,
        )

        if res is None:
            logger.error(
                "❌ send_order_common returned None symbol=%s",
                symbol,
            )
            return None

        if not isinstance(res, dict):
            logger.error(
                "❌ send_order_common invalid type symbol=%s type=%s",
                symbol,
                type(res),
            )
            return None

        if "OrderId" not in res:
            logger.error(
                "❌ kabu API error symbol=%s response=%s",
                symbol,
                res,
            )
            return None

        return res

    except Exception:
        logger.error(
            "❌ send_order_common exception symbol=%s",
            symbol,
            exc_info=True,
        )
        return None


# ============================================================
# qty resolve
# ============================================================

def _resolve_actual_qty(symbol, price, requested_qty) -> int:
    try:
        base_qty = _resolve_requested_qty(requested_qty)
        unit_qty = _normalize_qty_to_unit(symbol, base_qty)
        actual_qty = _enforce_oneshot_rule(symbol, price, unit_qty)

        logger.info(
            "[QTY RESOLVED] symbol=%s requested_qty=%s base_qty=%s unit_qty=%s actual_qty=%s price=%s max_oneshot=%s",
            symbol,
            requested_qty,
            base_qty,
            unit_qty,
            actual_qty,
            price,
            int(MAX_ONESHOT),
        )

        return actual_qty

    except Exception:
        logger.error("❌ _resolve_actual_qty エラー", exc_info=True)
        return 0


# ============================================================
# BUY（最良ASK指値）
# ============================================================

def execute_buy_at_best_ask(symbol, qty=None, lot_yen=700000, exchange=None):
    try:
        quotes = get_latest_bid_ask(symbol)

        if not quotes:
            logger.warning("⚠ %s: 板なし → スキップ", symbol)
            return None

        ask = quotes.get("ask_price")

        if not ask or ask <= 0:
            logger.warning("⚠ %s: ASK 不正 → スキップ", symbol)
            return None

        actual_qty = _resolve_actual_qty(symbol, ask, qty)

        if actual_qty <= 0:
            return None

        logger.info("🟢 信用新規買い ASK %s %s × %s", symbol, ask, actual_qty)

        payload = _make_payload(
            symbol,
            side=2,
            qty=actual_qty,
            price=ask,
            exchange=exchange,
            front_order_type=20,
        )

        res = _send_order(payload, symbol)

        if not res:
            return None

        return (res.get("OrderId"), res.get("Price", ask), actual_qty)

    except Exception:
        logger.error("❌ execute_buy_at_best_ask エラー", exc_info=True)
        return None


# ============================================================
# SELL（最良BID指値）
# ============================================================

def execute_short_at_best_bid(symbol, qty=None, lot_yen=700000, exchange=None):
    try:
        quotes = get_latest_bid_ask(symbol)

        if not quotes:
            logger.warning("⚠ %s: 板なし → スキップ", symbol)
            return None

        bid = quotes.get("bid_price")

        if not bid or bid <= 0:
            logger.warning("⚠ %s: BID 不正 → スキップ", symbol)
            return None

        actual_qty = _resolve_actual_qty(symbol, bid, qty)

        if actual_qty <= 0:
            return None

        logger.info("🔻 信用空売り BID %s %s × %s", symbol, bid, actual_qty)

        payload = _make_payload(
            symbol,
            side=1,
            qty=actual_qty,
            price=bid,
            exchange=exchange,
            front_order_type=20,
        )

        res = _send_order(payload, symbol)

        if not res:
            return None

        return (res.get("OrderId"), res.get("Price", bid), actual_qty)

    except Exception:
        logger.error("❌ execute_short_at_best_bid エラー", exc_info=True)
        return None


# ============================================================
# BUY（成行）
# ============================================================

def execute_buy_market(symbol, qty=None, reference_price=None, exchange=None):
    try:
        quotes = get_latest_bid_ask(symbol)
        price_for_guard = _resolve_market_reference_price(
            symbol,
            quotes,
            reference_price,
            side="BUY",
        )

        if price_for_guard <= 0:
            logger.warning("⚠ %s: MARKET BUY 参考価格なし → スキップ", symbol)
            return None

        actual_qty = _resolve_actual_qty(symbol, price_for_guard, qty)

        if actual_qty <= 0:
            return None

        logger.info(
            "[MARKET BUY] %s × %s reference_price=%s quotes_available=%s",
            symbol,
            actual_qty,
            price_for_guard,
            bool(quotes),
        )

        payload = _make_payload(
            symbol,
            side=2,
            qty=actual_qty,
            price=0,
            exchange=exchange,
            front_order_type=10,
        )

        res = _send_order(payload, symbol)

        if not res:
            return None

        return (res.get("OrderId"), res.get("Price", 0), actual_qty)

    except Exception:
        logger.error("❌ execute_buy_market エラー", exc_info=True)
        return None


# ============================================================
# SELL（成行）
# ============================================================

def execute_sell_market(symbol, qty=None, reference_price=None, exchange=None):
    try:
        quotes = get_latest_bid_ask(symbol)
        price_for_guard = _resolve_market_reference_price(
            symbol,
            quotes,
            reference_price,
            side="SELL",
        )

        if price_for_guard <= 0:
            logger.warning("⚠ %s: MARKET SELL 参考価格なし → スキップ", symbol)
            return None

        actual_qty = _resolve_actual_qty(symbol, price_for_guard, qty)

        if actual_qty <= 0:
            return None

        logger.info(
            "[MARKET SELL] %s × %s reference_price=%s quotes_available=%s",
            symbol,
            actual_qty,
            price_for_guard,
            bool(quotes),
        )

        payload = _make_payload(
            symbol,
            side=1,
            qty=actual_qty,
            price=0,
            exchange=exchange,
            front_order_type=10,
        )

        res = _send_order(payload, symbol)

        if not res:
            return None

        return (res.get("OrderId"), res.get("Price", 0), actual_qty)

    except Exception:
        logger.error("❌ execute_sell_market エラー", exc_info=True)
        return None


# ============================================================
# STOP BUY
# ============================================================

def execute_buy_stop(symbol: str, qty=None, stop_price: float = None, exchange=None):
    try:
        if stop_price is None:
            logger.warning("⚠ STOP BUY stop_price=None symbol=%s", symbol)
            return None

        actual_qty = _normalize_qty_to_unit(symbol, _resolve_requested_qty(qty))
        if actual_qty <= 0:
            return None

        logger.info("🟢 STOP BUY %s stop=%s qty=%s", symbol, stop_price, actual_qty)

        payload = _make_payload(
            symbol,
            side=2,
            qty=actual_qty,
            price=0,
            stop_price=stop_price,
            exchange=exchange,
            front_order_type=30,
        )

        res = _send_order(payload, symbol)

        if not res:
            return None

        return (res.get("OrderId"), stop_price, actual_qty)

    except Exception:
        logger.error("❌ execute_buy_stop エラー", exc_info=True)
        return None


# ============================================================
# STOP SELL
# ============================================================

def execute_short_stop(symbol: str, qty=None, stop_price: float = None, exchange=None):
    try:
        if stop_price is None:
            logger.warning("⚠ STOP SELL stop_price=None symbol=%s", symbol)
            return None

        actual_qty = _normalize_qty_to_unit(symbol, _resolve_requested_qty(qty))
        if actual_qty <= 0:
            return None

        logger.info("🔻 STOP SELL %s stop=%s qty=%s", symbol, stop_price, actual_qty)

        payload = _make_payload(
            symbol,
            side=1,
            qty=actual_qty,
            price=0,
            stop_price=stop_price,
            exchange=exchange,
            front_order_type=30,
        )

        res = _send_order(payload, symbol)

        if not res:
            return None

        return (res.get("OrderId"), stop_price, actual_qty)

    except Exception:
        logger.error("❌ execute_short_stop エラー", exc_info=True)
        return None


# ============================================================
# COMPATIBILITY WRAPPER
# (execution_engine互換)
# ============================================================

def buy_entry(symbol, qty=None, price=None):
    return execute_buy_at_best_ask(symbol, qty=qty)


def sell_entry(symbol, qty=None, price=None):
    return execute_short_at_best_bid(symbol, qty=qty)
