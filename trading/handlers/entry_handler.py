# ============================================================
# File   : trading/handlers/entry_handler.py
# Version: Ver27.10.0-FINAL-MARKET-REFERENCE-PRICE-RECOVERY
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
# ============================================================

import logging
from typing import Optional, Any

from global_state import global_data

from kabu_api.buy_sell_entry import (
    execute_buy_at_best_ask,
    execute_short_at_best_bid,
    execute_buy_market,
    execute_sell_market,
    execute_buy_stop,
    execute_short_stop,
)

logger = logging.getLogger("entry_handler")


# ============================================================
# 内部ユーティリティ
# ============================================================

def _ensure_entry_inflight_set():
    """
    entry_inflight が set であることを保証する
    """
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

    # 1) pending_entries の同一銘柄 bucket から復元
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

    # 2) get_merged_summary から復元
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

    # 3) よく使うglobal_data属性から復元
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

    # --- 後方互換（位置引数吸収） ---
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

            logger.info(
                "[ENTRY BUY DISPATCH] LIMIT symbol=%s qty=%s (best ask execution)",
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

            logger.info(
                "[ENTRY SELL DISPATCH] LIMIT symbol=%s qty=%s (best bid execution)",
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
    return
