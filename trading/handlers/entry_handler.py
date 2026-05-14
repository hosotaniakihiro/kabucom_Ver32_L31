# ============================================================
# File   : trading/handlers/entry_handler.py
# Version: Ver27.12.0-FINAL-LIMIT-PRICE-DIRECT-SEND-CANCEL-WATCH
# ------------------------------------------------------------
# ✔ kabu_api.buy_sell_entry に完全準拠
# ✔ 注文実行専用（低レイヤ）
# ✔ 判断・LOCK・AI 一切なし
# ✔ kabu_api の生戻り値を必ずログ出力
# ✔ 発注不可理由をログだけで100%特定可能
# ✔ pending_monitor 互換 (_unlock_entry)
# ✔ entry_inflight 型破壊を実運用で完全防止
# ✔ inflight の add は上位レイヤに一元化（重複防止）
# ✔ MARKET 注文 qty=None を即エラー化（静かな失敗防止）
# ✔ 位置引数 / キーワード引数 両対応（後方互換）
# ✔ qty passthrough を明示ログ化
# ✔ kabu low-layer 戻り値(order_id, price, qty)を診断ログ化
# ✔ MARKET注文へ上位レイヤpriceをreference_priceとして渡す
# ✔ order_price=None の場合は pending/global_data から reference_price を復元
# ✔ LIMIT注文で上位レイヤpriceがある場合は、その価格で直接発注する
# ✔ DIRECT LIMITでも信用新規は bse.ENTRY_EXCHANGE（通常27）を使う
# ✔ 発注成功OrderIdを10秒取消監視へ登録
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Any

from global_state import global_data

import kabu_api.buy_sell_entry as bse

from kabu_api.buy_sell_entry import (
    execute_buy_at_best_ask,
    execute_short_at_best_bid,
    execute_buy_market,
    execute_sell_market,
    execute_buy_stop,
    execute_short_stop,
)

try:
    from trading.handlers.pending_order_monitor import register_pending_entry_order
except Exception:  # 起動時の循環/部分導入でも落とさない
    register_pending_entry_order = None

logger = logging.getLogger("entry_handler")


# ============================================================
# 内部ユーティリティ
# ============================================================

def _ensure_entry_inflight_set():
    if not isinstance(global_data.entry_inflight, set):
        logger.critical(
            "[ENTRY_INFLIGHT CORRUPTED] expected set, got %s → auto-fix",
            type(global_data.entry_inflight),
        )
        global_data.entry_inflight = set()


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        q = int(float(v))
        return q if q > 0 else None
    except Exception:
        return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        p = float(v)
        return p if p > 0 else None
    except Exception:
        return None


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _normalize_result(res):
    """
    kabu_api の戻り値を正規化
    res: (order_id, price, qty) or None
    """
    if res is None:
        logger.error("[KABU RAW RESULT] None")
        return None

    logger.info("[KABU RAW RESULT] %s", res)

    try:
        order_id, price, qty = res
    except Exception as e:
        logger.error("[KABU RAW PARSE ERROR] res=%s error=%s", res, e)
        return None

    if not order_id:
        logger.error("[KABU RAW INVALID] order_id empty res=%s", res)
        return None

    logger.info(
        "[KABU NORMALIZED RESULT] order_id=%s executed_price=%s executed_qty=%s",
        order_id,
        price,
        qty,
    )

    return order_id


def _safe_qty(qty: Optional[int]) -> Optional[int]:
    return _safe_int(qty)


def _safe_price(price: Any) -> Optional[float]:
    return _safe_float(price)


def _pick_price_from_df(df, symbol: str) -> Optional[float]:
    try:
        if df is None or not hasattr(df, "empty") or df.empty:
            return None
        if "symbol" not in getattr(df, "columns", []):
            return None

        sym = _norm_symbol(symbol)
        x = df.copy()
        x["_sym_norm"] = x["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        x = x[x["_sym_norm"] == sym]
        if x.empty:
            return None

        if "datetime" in x.columns:
            try:
                x = x.sort_values("datetime")
            except Exception:
                pass

        row = x.iloc[-1]
        for col in ("close_price", "price", "current_price", "close"):
            if col in x.columns:
                p = _safe_float(row.get(col))
                if p and p > 0:
                    return p
        return None
    except Exception:
        return None


def _recover_reference_price(symbol: str) -> Optional[float]:
    """
    entry_controller の order_price が MARKET で None になった場合の保険。
    pending_entries と global_data の最新summaryから価格を復元する。
    """
    sym = _norm_symbol(symbol)

    try:
        root = getattr(global_data, "pending_entries", {}) or {}
        bucket = root.get(sym) or root.get(str(symbol)) or []
        for e in reversed(list(bucket)):
            if not isinstance(e, dict):
                continue
            for col in ("close_price", "price", "current_price", "close"):
                p = _safe_float(e.get(col))
                if p and p > 0:
                    logger.warning(
                        "[ENTRY PRICE RECOVER] symbol=%s source=pending col=%s price=%s",
                        sym,
                        col,
                        p,
                    )
                    return p
    except Exception:
        pass

    try:
        getter = getattr(global_data, "get_merged_summary", None)
        if callable(getter):
            for tf in (1, 3, 5):
                for source in ("push", "SUMMARY", None):
                    try:
                        df = getter(tf=tf, source=source) if source is not None else getter(tf=tf)
                    except TypeError:
                        try:
                            df = getter(tf)
                        except Exception:
                            df = None
                    except Exception:
                        df = None
                    p = _pick_price_from_df(df, sym)
                    if p and p > 0:
                        logger.warning(
                            "[ENTRY PRICE RECOVER] symbol=%s source=get_merged_summary tf=%s src=%s price=%s",
                            sym,
                            tf,
                            source,
                            p,
                        )
                        return p
    except Exception:
        pass

    attr_names = []
    for tf in (1, 3, 5):
        attr_names.extend([
            f"push_merged_summary_{tf}min",
            f"push_summary_{tf}min",
            f"merged_summary_{tf}min",
            f"summary_{tf}min",
            f"push_merged_summary_{tf}m",
            f"push_summary_{tf}m",
        ])

    for name in attr_names:
        try:
            df = getattr(global_data, name, None)
            p = _pick_price_from_df(df, sym)
            if p and p > 0:
                logger.warning(
                    "[ENTRY PRICE RECOVER] symbol=%s source=global_data.%s price=%s",
                    sym,
                    name,
                    p,
                )
                return p
        except Exception:
            continue

    return None


def _normalize_args(
    symbol: str,
    symbolname: str,
    price: Optional[float],
    reason: str,
    order_type: Optional[str],
    qty: Optional[int],
):
    sym = str(symbol).strip() if symbol is not None else ""
    p = _safe_price(price)
    ot = (order_type or "LIMIT").upper()

    if p is None and ot == "MARKET" and sym:
        p = _recover_reference_price(sym)

    return {
        "symbol": sym,
        "symbolname": symbolname or "",
        "price": p,
        "reason": reason or "",
        "order_type": ot,
        "qty": _safe_qty(qty),
    }


def _register_cancel_watch(order_id: str, symbol: str, side: str, qty: Optional[int], price: Optional[float], source: str):
    try:
        if callable(register_pending_entry_order):
            register_pending_entry_order(
                order_id=order_id,
                symbol=symbol,
                side=side,
                qty=int(qty or 0),
                price=price,
                source=source,
            )
        else:
            logger.warning(
                "[ENTRY CANCEL WATCH] register function unavailable order_id=%s symbol=%s side=%s",
                order_id,
                symbol,
                side,
            )
    except Exception:
        logger.exception(
            "[ENTRY CANCEL WATCH] register failed order_id=%s symbol=%s side=%s",
            order_id,
            symbol,
            side,
        )


def _execute_direct_limit_order(symbol: str, side: str, price: float, qty: int):
    """
    entry_order_builder が price 付き LIMIT を作った場合は、板を取り直さずに
    その指値で直接 payload を作る。

    これにより、summary_fallback_limit の price があるにもかかわらず
    execute_buy_at_best_ask / execute_short_at_best_bid が板なしでスキップする問題を防ぐ。

    信用新規エントリーの市場は kabu_api.buy_sell_entry.ENTRY_EXCHANGE を使う。
    通常時の信用新規は Exchange=27（東証+）。
    """
    try:
        side_u = str(side or "").upper()
        side_code = 2 if side_u == "BUY" else 1
        px = float(price)
        q = int(qty)

        if px <= 0 or q <= 0:
            logger.error(
                "[ENTRY DIRECT LIMIT INVALID] symbol=%s side=%s price=%s qty=%s",
                symbol,
                side_u,
                price,
                qty,
            )
            return None

        entry_exchange = int(getattr(bse, "ENTRY_EXCHANGE", 27))

        logger.warning(
            "[ENTRY DIRECT LIMIT DISPATCH] symbol=%s side=%s price=%s qty=%s exchange=%s reason=use_order_builder_limit_price",
            symbol,
            side_u,
            px,
            q,
            entry_exchange,
        )

        payload = bse._make_payload(
            symbol,
            side=side_code,
            qty=q,
            price=px,
            exchange=entry_exchange,
            front_order_type=20,
        )

        res = bse._send_order(payload, symbol)
        if not res:
            return None

        return (res.get("OrderId"), res.get("Price", px), q)

    except Exception:
        logger.exception("[ENTRY DIRECT LIMIT EXCEPTION] symbol=%s side=%s", symbol, side)
        return None


# ============================================================
# BUY
# ============================================================

def place_entry_buy(
    symbol: str,
    symbolname: str,
    price: Optional[float],
    reason: str,
    *args,
    order_type: str = "LIMIT",
    qty: Optional[int] = None,
):
    """
    BUY 新規エントリー（後方互換対応）
    """

    if args:
        if len(args) >= 1:
            order_type = args[0]
        if len(args) >= 2:
            qty = args[1]

    p = _normalize_args(symbol, symbolname, price, reason, order_type, qty)

    logger.info(
        "[ENTRY BUY TRY] symbol=%s type=%s price=%s qty=%s reason=%s",
        p["symbol"], p["order_type"], p["price"], p["qty"], p["reason"],
    )

    try:
        if not p["symbol"]:
            raise ValueError("BUY requires symbol")

        if p["order_type"] == "MARKET":
            if p["qty"] is None:
                raise ValueError("MARKET BUY requires qty")

            logger.info(
                "[ENTRY BUY DISPATCH] MARKET symbol=%s qty=%s reference_price=%s",
                p["symbol"], p["qty"], p["price"],
            )
            res = execute_buy_market(p["symbol"], p["qty"], reference_price=p["price"])

        elif p["order_type"] == "STOP":
            if p["price"] is None or p["qty"] is None:
                raise ValueError("STOP BUY requires price & qty")

            logger.info(
                "[ENTRY BUY DISPATCH] STOP symbol=%s stop_price=%s qty=%s",
                p["symbol"], p["price"], p["qty"],
            )
            res = execute_buy_stop(p["symbol"], p["qty"], p["price"])

        else:
            if p["qty"] is None:
                raise ValueError("LIMIT BUY requires qty")

            if p["price"] is not None:
                logger.info(
                    "[ENTRY BUY DISPATCH] LIMIT symbol=%s price=%s qty=%s direct_limit=True",
                    p["symbol"], p["price"], p["qty"],
                )
                res = _execute_direct_limit_order(p["symbol"], "BUY", p["price"], p["qty"])
            else:
                logger.info(
                    "[ENTRY BUY DISPATCH] LIMIT symbol=%s qty=%s best_ask_fallback=True",
                    p["symbol"], p["qty"],
                )
                res = execute_buy_at_best_ask(p["symbol"], p["qty"])

        order_id = _normalize_result(res)
        if not order_id:
            logger.error(
                "[ENTRY BUY FAILED] symbol=%s type=%s qty=%s reason=%s",
                p["symbol"], p["order_type"], p["qty"], p["reason"],
            )
            return None

        _ensure_entry_inflight_set()
        _register_cancel_watch(order_id, p["symbol"], "BUY", p["qty"], p["price"], "ENTRY_BUY")

        logger.info(
            "[ENTRY BUY SENT] symbol=%s type=%s oid=%s qty=%s reason=%s",
            p["symbol"], p["order_type"], order_id, p["qty"], p["reason"],
        )
        return order_id

    except Exception:
        logger.exception("[ENTRY BUY EXCEPTION] %s", symbol)
        return None


# ============================================================
# SELL
# ============================================================

def place_entry_sell(
    symbol: str,
    symbolname: str,
    price: Optional[float],
    reason: str,
    *args,
    order_type: str = "LIMIT",
    qty: Optional[int] = None,
):
    """
    SELL 新規エントリー（後方互換対応）
    """

    if args:
        if len(args) >= 1:
            order_type = args[0]
        if len(args) >= 2:
            qty = args[1]

    p = _normalize_args(symbol, symbolname, price, reason, order_type, qty)

    logger.info(
        "[ENTRY SELL TRY] symbol=%s type=%s price=%s qty=%s reason=%s",
        p["symbol"], p["order_type"], p["price"], p["qty"], p["reason"],
    )

    try:
        if not p["symbol"]:
            raise ValueError("SELL requires symbol")

        if p["order_type"] == "MARKET":
            if p["qty"] is None:
                raise ValueError("MARKET SELL requires qty")

            logger.info(
                "[ENTRY SELL DISPATCH] MARKET symbol=%s qty=%s reference_price=%s",
                p["symbol"], p["qty"], p["price"],
            )
            res = execute_sell_market(p["symbol"], p["qty"], reference_price=p["price"])

        elif p["order_type"] == "STOP":
            if p["price"] is None or p["qty"] is None:
                raise ValueError("STOP SELL requires price & qty")

            logger.info(
                "[ENTRY SELL DISPATCH] STOP symbol=%s stop_price=%s qty=%s",
                p["symbol"], p["price"], p["qty"],
            )
            res = execute_short_stop(p["symbol"], p["qty"], p["price"])

        else:
            if p["qty"] is None:
                raise ValueError("LIMIT SELL requires qty")

            if p["price"] is not None:
                logger.info(
                    "[ENTRY SELL DISPATCH] LIMIT symbol=%s price=%s qty=%s direct_limit=True",
                    p["symbol"], p["price"], p["qty"],
                )
                res = _execute_direct_limit_order(p["symbol"], "SELL", p["price"], p["qty"])
            else:
                logger.info(
                    "[ENTRY SELL DISPATCH] LIMIT symbol=%s qty=%s best_bid_fallback=True",
                    p["symbol"], p["qty"],
                )
                res = execute_short_at_best_bid(p["symbol"], p["qty"])

        order_id = _normalize_result(res)
        if not order_id:
            logger.error(
                "[ENTRY SELL FAILED] symbol=%s type=%s qty=%s reason=%s",
                p["symbol"], p["order_type"], p["qty"], p["reason"],
            )
            return None

        _ensure_entry_inflight_set()
        _register_cancel_watch(order_id, p["symbol"], "SELL", p["qty"], p["price"], "ENTRY_SELL")

        logger.info(
            "[ENTRY SELL SENT] symbol=%s type=%s oid=%s qty=%s reason=%s",
            p["symbol"], p["order_type"], order_id, p["qty"], p["reason"],
        )
        return order_id

    except Exception:
        logger.exception("[ENTRY SELL EXCEPTION] %s", symbol)
        return None


# ============================================================
# pending_monitor 互換
# ============================================================

def _unlock_entry(symbol: str):
    try:
        sym = _norm_symbol(symbol)
        inflight = getattr(global_data, "entry_inflight", None)
        if hasattr(inflight, "discard"):
            inflight.discard(sym)
    except Exception:
        pass
    return
