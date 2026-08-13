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

import datetime as dt
import logging
import configparser
import os
from typing import Any, Optional

from utils_common import (
    get_latest_bid_ask as _get_latest_bid_ask_core,
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


# ============================================================
# ENTRY FRESH QUOTE GUARD
# (旧 core/startup/entry_fresh_quote_guard_patch.py を本文化)
# ------------------------------------------------------------
# 発注前の気配を PUSH の最新値から作り直し、古い気配での発注を防ぐ。
# 鮮度が足りなければ None を返して呼び出し側に見送らせる。
#
# 実測: kabu_api.buy_sell_entry.get_latest_bid_ask を差し替えていたのは
# このパッチだけで (board_retry_patch / summary_ai_strict_board_rest_patch は
# 名前に言及するだけ)、しかも元関数を呼ばない**完全置換**だった。
# よって本文でも同じく置換にしてある。
# utils_common 側の実装は _get_latest_bid_ask_core として残してあるが、
# 旧構成と同じくここからは呼ばない。
#
# 有効/無効は旧 install と同じ ENTRY_FRESH_QUOTE_GUARD_ENABLED (既定 True)。
# ============================================================

_FQG__INSTALLED = False
_FQG__ORIG_BSE_GET_LATEST_BID_ASK = None


def _fqg_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _fqg_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _fqg_norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _fqg_to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _fqg_parse_dt(v: Any) -> Optional[dt.datetime]:
    if v is None:
        return None
    try:
        import pandas as pd
        t = pd.to_datetime(v, errors="coerce")
        if pd.isna(t):
            return None
        # pandas Timestamp -> python datetime
        if hasattr(t, "to_pydatetime"):
            t = t.to_pydatetime()
        if isinstance(t, dt.datetime):
            # timezone awareの場合はnaiveに揃える
            if t.tzinfo is not None:
                t = t.astimezone().replace(tzinfo=None)
            return t
    except Exception:
        pass
    return None


def _fqg_latest_push_quote(symbol: str) -> Optional[dict[str, Any]]:
    symbol = _fqg_norm_symbol(symbol)
    if not symbol:
        return None
    try:
        from global_state import global_data

        dfp = global_data.get_push_df()
        if dfp is None or getattr(dfp, "empty", True):
            return None

        df = dfp.copy()
        rename_map = {
            "Symbol": "symbol",
            "BidPrice": "bid_price",
            "AskPrice": "ask_price",
            "BidQty": "bid_qty",
            "AskQty": "ask_qty",
            "CurrentPrice": "current_price",
            "CurrentPriceTime": "datetime",
            "time": "datetime",
            "DateTime": "datetime",
            "datetime": "datetime",
        }
        for old, new in rename_map.items():
            if old in df.columns and new not in df.columns:
                df[new] = df[old]

        if "symbol" not in df.columns:
            return None
        try:
            df["_sym_norm"] = df["symbol"].map(_fqg_norm_symbol)
            df_sym = df[df["_sym_norm"] == symbol]
        except Exception:
            df_sym = df[df["symbol"].astype(str) == symbol]

        if df_sym.empty:
            return None

        dt_col = None
        for c in ("datetime", "CurrentPriceTime", "time", "timestamp", "updated_at"):
            if c in df_sym.columns:
                dt_col = c
                break

        row = None
        quote_dt = None
        if dt_col:
            import pandas as pd
            x = df_sym.copy()
            x["_quote_dt"] = pd.to_datetime(x[dt_col], errors="coerce")
            x2 = x.dropna(subset=["_quote_dt"])
            if not x2.empty:
                row = x2.sort_values("_quote_dt").iloc[-1]
                quote_dt = _fqg_parse_dt(row.get("_quote_dt"))
        if row is None:
            row = df_sym.iloc[-1]
            quote_dt = None

        ask = _fqg_to_float(row.get("ask_price"), 0.0)
        bid = _fqg_to_float(row.get("bid_price"), 0.0)
        cur = _fqg_to_float(row.get("current_price") or row.get("price") or row.get("CurrentPrice"), 0.0)
        if ask <= 0 or bid <= 0:
            return None

        now = dt.datetime.now()
        age_sec = None
        if quote_dt is not None:
            # 日付が無い時刻だけの場合、古い年になることがあるので当日補正
            if quote_dt.year < 2000:
                quote_dt = now.replace(hour=quote_dt.hour, minute=quote_dt.minute, second=quote_dt.second, microsecond=quote_dt.microsecond)
            age_sec = max(0.0, (now - quote_dt).total_seconds())

        return {
            "symbol": symbol,
            "ask_price": ask,
            "bid_price": bid,
            "ask_qty": int(_fqg_to_float(row.get("ask_qty"), 0.0)),
            "bid_qty": int(_fqg_to_float(row.get("bid_qty"), 0.0)),
            "current_price": cur,
            "quote_dt": quote_dt,
            "age_sec": age_sec,
            "source": "push_df_fresh_guard",
        }
    except Exception as e:
        logger.warning("[ENTRY FRESH QUOTE] read failed symbol=%s err=%s", symbol, e, exc_info=False)
        return None


def _fqg_fresh_quote(symbol: str, *, side: str) -> Optional[dict[str, Any]]:
    q = _fqg_latest_push_quote(symbol)
    max_age = _fqg_env_float("ENTRY_MAX_QUOTE_AGE_SEC", 8.0)
    if not q:
        logger.warning("[ENTRY FRESH QUOTE] SKIP no_quote symbol=%s side=%s", symbol, side)
        return None
    age = q.get("age_sec")
    # datetimeが取れない場合は危険なので、デフォルトでは古い扱いにする
    if age is None:
        if _fqg_env_bool("ENTRY_ALLOW_QUOTE_WITHOUT_TIMESTAMP", False):
            logger.warning("[ENTRY FRESH QUOTE] timestamp missing but allowed symbol=%s side=%s q=%s", symbol, side, q)
            return q
        logger.warning("[ENTRY FRESH QUOTE] SKIP no_timestamp symbol=%s side=%s q=%s", symbol, side, q)
        return None
    if float(age) > max_age:
        logger.warning(
            "[ENTRY FRESH QUOTE] SKIP stale_quote symbol=%s side=%s age=%.3fs max=%.3fs bid=%s ask=%s current=%s quote_dt=%s",
            symbol, side, float(age), max_age, q.get("bid_price"), q.get("ask_price"), q.get("current_price"), q.get("quote_dt"),
        )
        return None
    return q


def get_latest_bid_ask(symbol: str):
    if not _fqg_env_bool("ENTRY_FRESH_QUOTE_GUARD_ENABLED", True):
        return _get_latest_bid_ask_core(symbol)
    q = _fqg_fresh_quote(symbol, side="UNKNOWN")
    if not q:
        return None
    return {
        "symbol": q.get("symbol"),
        "ask_price": q.get("ask_price"),
        "bid_price": q.get("bid_price"),
        "ask_qty": q.get("ask_qty", 0),
        "bid_qty": q.get("bid_qty", 0),
        "current_price": q.get("current_price", 0.0),
        "age_sec": q.get("age_sec"),
        "quote_dt": q.get("quote_dt"),
        "source": q.get("source"),
    }


# ============================================================
# ONESHOT LIMIT / 起動時 env 既定値
# (旧 core/startup/oneshot_limit_700k_patch.py を本文化)
# ------------------------------------------------------------
# 旧パッチは 19 個のサブ install を持つランチャーだったが、実体は
#   - env 既定値 45 件 (setdefault が中心)
#   - entry_controller のスコアしきい値 4 定数
#   - kabu_api.buy_sell_entry.MAX_ONESHOT = 700_000
#   - 生きている連鎖 install 3 件
# だけだった (他 15 個は本文化済みの空スタブか env のみ)。
#
# **import 時ではなく main.py の patches リストの旧位置 (:250) から呼ぶこと。**
# entry_controller のしきい値は tonosama_history_failclose_strict_patch の
# watcher が 3.0 へ書き戻す関係があり、位置を変えると最終値が変わる。
# ============================================================

_OL7_ENV_SET: list[str] = []


def _ol7_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def apply_oneshot_limit_defaults() -> bool:
    """旧 oneshot_limit_700k_patch.install() の実体。"""
    global MAX_ONESHOT
    os.environ.setdefault("MAX_ENTRY_ONESHOT_YEN", "700000")
    os.environ.setdefault("ENTRY_LOW_PRICE_ONESHOT_YEN", "700000")
    os.environ.setdefault("ENTRY_HIGH_PRICE_ONESHOT_YEN", "700000")
    os.environ.setdefault("ENTRY_MAX_PRICE", "7000")
    os.environ.setdefault("ORDER_LOT_SIZE", "100")
    os.environ.setdefault("ENTRY_AFFORDABILITY_FILTER_ENABLED", "1")
    os.environ.setdefault("PUSH_BOOTSTRAP_FAST_RESTORE", "1")
    os.environ.setdefault("PUSH_BOOTSTRAP_FAST_MAX_ROWS", "3000")
    os.environ.setdefault("PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES", "45")
    os.environ.setdefault("ENTRY_FRESH_QUOTE_GUARD_ENABLED", "1")
    os.environ.setdefault("ENTRY_MAX_QUOTE_AGE_SEC", "8.0")
    os.environ.setdefault("ENTRY_ALLOW_QUOTE_WITHOUT_TIMESTAMP", "0")
    os.environ.setdefault("ENTRY_MIN_LOT_FALLBACK_WHEN_AFFORDABLE", "1")
    os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
    os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", "0")
    os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", "20")
    os.environ.setdefault("SUMMARY_AI_STRICT_LIQ_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_LIQ_MIN_LATEST_VOLUME", "30000")
    os.environ.setdefault("SUMMARY_AI_LIQ_MIN_AVG_VOLUME", "30000")
    os.environ.setdefault("WATCHLIST_RECENT_LIQ_ENABLED", "1")
    os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", "3000")
    os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", "3000")
    os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", "1000000")
    os.environ.setdefault("WATCHLIST_RECENT_LIQ_BARS", "5")
    os.environ.setdefault("WATCHLIST_RECENT_LIQ_PROTECT_BYPASS", "1")
    os.environ.setdefault("WATCHLIST_RECENT_LIQ_CACHE_TTL_SEC", "10")
    os.environ.setdefault("WATCHLIST_RECENT_LIQ_BULK_LOG_SEC", "0.5")
    os.environ.setdefault("SUMMARY_PARALLEL_INTERVALS_ENABLED", "1")
    os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_WORKERS", "3")
    os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", "55")
    os.environ.setdefault("SUMMARY_PARALLEL_RANKING_ENABLED", "1")
    os.environ.setdefault("ENTRY_PIPELINE_BUCKET_PREFILTER", "1")
    os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_VOLUME", "30000")
    os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_TURNOVER", "10000000")
    os.environ.setdefault("SUMMARY_AI_ATR_FAILOPEN_MIN_PRICE", "1500")
    os.environ.setdefault("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI", "1")
    os.environ.setdefault("EXIT_BOARD_PROFIT_ENABLED", "1")
    os.environ.setdefault("EXIT_BOARD_MIN_PROFIT_PCT", "0.0010")
    os.environ.setdefault("EXIT_BOARD_THICK_QTY_MULT", "3.0")
    os.environ.setdefault("EXIT_BOARD_MIN_THICK_QTY", "1000")
    os.environ.setdefault("EXIT_BOARD_MAX_WAIT_SEC", "2.0")
    os.environ.setdefault("EXIT_BOARD_POLL_SEC", "0.25")
    os.environ.setdefault("EXIT_BOARD_REVERSE_GAP_PCT", "0.0010")

    try:
        old_value = globals().get("MAX_ONESHOT")
        MAX_ONESHOT = 700_000
        logger.warning("[ONESHOT LIMIT PATCH] MAX_ONESHOT changed old=%s new=%s", old_value, MAX_ONESHOT)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] MAX_ONESHOT apply failed")

    try:
        import trading.handlers.entry_controller as ec

        old = (getattr(ec, "MIN_SUMMARY_SCORE_BUY", None), getattr(ec, "MIN_COMPOSITE_SCORE_BUY", None),
               getattr(ec, "MIN_SUMMARY_SCORE_SELL", None), getattr(ec, "MIN_COMPOSITE_SCORE_SELL", None))
        ec.MIN_SUMMARY_SCORE_BUY = _ol7_env_float("MIN_SUMMARY_SCORE_BUY", 1.0)
        ec.MIN_COMPOSITE_SCORE_BUY = _ol7_env_float("MIN_COMPOSITE_SCORE_BUY", 0.8)
        ec.MIN_SUMMARY_SCORE_SELL = _ol7_env_float("MIN_SUMMARY_SCORE_SELL", 1.0)
        ec.MIN_COMPOSITE_SCORE_SELL = _ol7_env_float("MIN_COMPOSITE_SCORE_SELL", 1.0)
        logger.warning("[ENTRY THRESHOLD PATCH] installed BUY score %s->%s comp %s->%s SELL score %s->%s comp %s->%s",
                       old[0], ec.MIN_SUMMARY_SCORE_BUY, old[1], ec.MIN_COMPOSITE_SCORE_BUY,
                       old[2], ec.MIN_SUMMARY_SCORE_SELL, old[3], ec.MIN_COMPOSITE_SCORE_SELL)
    except Exception:
        logger.exception("[ENTRY THRESHOLD PATCH] install failed")

    for _mod_name, _label in (
        ("core.startup.summary_ai_async_entry_patch", "summary_ai_async_entry"),
        # summary_parallel_intervals は scheduler_jobs/summary/parallel_intervals.py へ
        # 移設済みで、env 既定値も import 時に入るため連鎖 install は不要。
        ("core.startup.exit_board_profit_runtime_patch", "exit_board_profit"),
    ):
        try:
            _m = __import__(_mod_name, fromlist=["install"])
            _fn = getattr(_m, "install", None)
            _ok = bool(_fn()) if callable(_fn) else False
            logger.warning("[ONESHOT LIMIT PATCH] %s installed=%s", _label, _ok)
        except Exception:
            logger.exception("[ONESHOT LIMIT PATCH] %s install failed", _label)
    return True
