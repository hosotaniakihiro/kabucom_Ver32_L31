# ============================================================
# File   : trading/handlers/entry_handler.py
# Version: Ver27.13.0-FINAL-RECENT-VOLUME-GUARD-BEFORE-SEND
# ------------------------------------------------------------
# ✔ kabu_api.buy_sell_entry に完全準拠
# ✔ 注文実行専用（低レイヤ）
# ✔ pending_monitor 互換 (_unlock_entry)
# ✔ LIMIT注文で上位レイヤpriceがある場合は、その価格で直接発注する
# ✔ 発注成功OrderIdを10秒取消監視へ登録
#
# Ver27.13.0:
#   - 発注直前の最終安全弁として、BUY/SELL 共通で直近出来高を確認
#   - SUMMARY AI / RANKING / TONOSAMA など上位経路を問わずここで必ず止める
#   - 最新1分足 volume=0 は即NG
#   - 直近N本合計 volume / turnover が閾値未満ならNG
#   - DBが読めない場合は既定で fail-closed
#
# ENV:
#   ENTRY_HANDLER_RECENT_LIQ_GUARD_ENABLED=1
#   ENTRY_HANDLER_RECENT_LIQ_BARS=5
#   ENTRY_HANDLER_RECENT_LIQ_MIN_LATEST_VOLUME=1
#   ENTRY_HANDLER_RECENT_LIQ_MIN_VOLUME=30000
#   ENTRY_HANDLER_RECENT_LIQ_MIN_TURNOVER_YEN=10000000
#   ENTRY_HANDLER_RECENT_LIQ_REQUIRE_DATA=1
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
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
except Exception:
    register_pending_entry_order = None

logger = logging.getLogger("entry_handler")


# ============================================================
# 内部ユーティリティ
# ============================================================

def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


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


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _normalize_result(res):
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
    logger.info("[KABU NORMALIZED RESULT] order_id=%s executed_price=%s executed_qty=%s", order_id, price, qty)
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
                    logger.warning("[ENTRY PRICE RECOVER] symbol=%s source=pending col=%s price=%s", sym, col, p)
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
                        logger.warning("[ENTRY PRICE RECOVER] symbol=%s source=get_merged_summary tf=%s src=%s price=%s", sym, tf, source, p)
                        return p
    except Exception:
        pass
    attr_names = []
    for tf in (1, 3, 5):
        attr_names.extend([
            f"push_merged_summary_{tf}min", f"push_summary_{tf}min", f"merged_summary_{tf}min", f"summary_{tf}min",
            f"push_merged_summary_{tf}m", f"push_summary_{tf}m",
        ])
    for name in attr_names:
        try:
            df = getattr(global_data, name, None)
            p = _pick_price_from_df(df, sym)
            if p and p > 0:
                logger.warning("[ENTRY PRICE RECOVER] symbol=%s source=global_data.%s price=%s", sym, name, p)
                return p
        except Exception:
            continue
    return None


def _normalize_args(symbol: str, symbolname: str, price: Optional[float], reason: str, order_type: Optional[str], qty: Optional[int]):
    sym = str(symbol).strip() if symbol is not None else ""
    p = _safe_price(price)
    ot = (order_type or "LIMIT").upper()
    if p is None and ot == "MARKET" and sym:
        p = _recover_reference_price(sym)
    return {"symbol": sym, "symbolname": symbolname or "", "price": p, "reason": reason or "", "order_type": ot, "qty": _safe_qty(qty)}


def _summary_db_path() -> str:
    base = os.getenv("SUMMARY_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary")
    today = dt.datetime.now().strftime("%Y%m%d")
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{today}.db"))


def _col(conn: sqlite3.Connection, table: str, names: list[str]) -> str:
    try:
        cols = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        for n in names:
            if n in cols:
                return n
    except Exception:
        return ""
    return ""


def _recent_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    path = _summary_db_path()
    table = os.getenv("ENTRY_HANDLER_RECENT_LIQ_TABLE", os.getenv("SUMMARY_AI_LIQ_SUMMARY_TABLE", "stock_summary_1min"))
    if not symbol or not Path(path).exists():
        return {"ok_read": False, "reason": "summary_db_missing", "summary_db": path, "table": table}
    try:
        with sqlite3.connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            sym = _col(conn, table, ["symbol", "code", "stock_code"])
            tm = _col(conn, table, ["datetime", "dt", "timestamp", "time"])
            cl = _col(conn, table, ["close_price", "close", "price", "current_price"])
            vo = _col(conn, table, ["volume", "Volume", "vol", "出来高"])
            tv = _col(conn, table, ["turnover_yen", "turnover", "trading_value", "売買代金"])
            if not sym or not tm or not cl or not vo:
                return {"ok_read": False, "reason": "required_columns_missing", "summary_db": path, "table": table, "sym_col": sym, "tm_col": tm, "close_col": cl, "volume_col": vo}
            rows = conn.execute(
                f'SELECT {tm}, {cl}, {vo}, {tv or "0"} FROM "{table}" WHERE CAST({sym} AS TEXT)=? ORDER BY {tm} DESC LIMIT ?',
                (_norm_symbol(symbol), max(1, int(bars))),
            ).fetchall()
        if not rows:
            return {"ok_read": True, "rows": 0, "reason": "no_recent_rows", "summary_db": path, "table": table}
        latest_close = _f(rows[0][1], 0.0)
        latest_volume = _f(rows[0][2], 0.0)
        latest_turnover_raw = _f(rows[0][3], 0.0)
        volumes = [max(0.0, _f(r[2], 0.0)) for r in rows]
        closes = [max(0.0, _f(r[1], 0.0)) for r in rows]
        turnover_raws = [max(0.0, _f(r[3], 0.0)) for r in rows]
        volume_sum = float(sum(volumes))
        turnover_raw_sum = float(sum(turnover_raws))
        turnover_calc = float(sum(c * v for c, v in zip(closes, volumes) if c > 0 and v > 0))
        latest_turnover = max(latest_turnover_raw, latest_close * latest_volume if latest_close > 0 and latest_volume > 0 else 0.0)
        return {
            "ok_read": True,
            "rows": len(rows),
            "summary_db": path,
            "table": table,
            "latest_dt": str(rows[0][0]),
            "latest_close": latest_close,
            "latest_volume": latest_volume,
            "latest_turnover": latest_turnover,
            "volume_sum": volume_sum,
            "turnover_sum": max(turnover_raw_sum, turnover_calc),
            "turnover_raw_sum": turnover_raw_sum,
            "turnover_calc": turnover_calc,
        }
    except Exception as e:
        logger.debug("[ENTRY FINAL LIQ GUARD] recent read failed symbol=%s", symbol, exc_info=True)
        return {"ok_read": False, "reason": "exception", "error": str(e), "summary_db": path, "table": table}


def _final_recent_liquidity_ok(symbol: str, side: str) -> tuple[bool, str, dict[str, Any]]:
    if not _env_bool("ENTRY_HANDLER_RECENT_LIQ_GUARD_ENABLED", True):
        return True, "disabled", {}
    bars = max(1, _env_int("ENTRY_HANDLER_RECENT_LIQ_BARS", 5))
    min_latest_volume = _env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_LATEST_VOLUME", 1.0)
    min_volume = _env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_VOLUME", _env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_TURNOVER_YEN", _env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 10000000.0))
    require_data = _env_bool("ENTRY_HANDLER_RECENT_LIQ_REQUIRE_DATA", True)
    v = _recent_liquidity_values(symbol, bars)
    detail = {"symbol": symbol, "side": side, "bars": bars, "min_latest_volume": min_latest_volume, "min_volume": min_volume, "min_turnover": min_turnover, "require_data": require_data, **v}
    if not bool(v.get("ok_read")):
        return (False, f"FINAL_LIQ_READ_NG:{v.get('reason')}", detail) if require_data else (True, "FINAL_LIQ_READ_FAIL_OPEN", detail)
    if int(v.get("rows") or 0) <= 0:
        return False, "FINAL_LIQ_NO_RECENT_ROWS", detail
    if _f(v.get("latest_volume"), 0.0) < min_latest_volume:
        return False, f"FINAL_LIQ_LATEST_VOLUME_LOW:{_f(v.get('latest_volume'), 0.0):.0f}<{min_latest_volume:.0f}", detail
    if _f(v.get("volume_sum"), 0.0) < min_volume:
        return False, f"FINAL_LIQ_VOLUME_LOW:{_f(v.get('volume_sum'), 0.0):.0f}<{min_volume:.0f}", detail
    if _f(v.get("turnover_sum"), 0.0) < min_turnover:
        return False, f"FINAL_LIQ_TURNOVER_LOW:{_f(v.get('turnover_sum'), 0.0):.0f}<{min_turnover:.0f}", detail
    return True, "FINAL_LIQ_OK", detail


def _register_cancel_watch(order_id: str, symbol: str, side: str, qty: Optional[int], price: Optional[float], source: str):
    try:
        if callable(register_pending_entry_order):
            register_pending_entry_order(order_id=order_id, symbol=symbol, side=side, qty=int(qty or 0), price=price, source=source)
        else:
            logger.warning("[ENTRY CANCEL WATCH] register function unavailable order_id=%s symbol=%s side=%s", order_id, symbol, side)
    except Exception:
        logger.exception("[ENTRY CANCEL WATCH] register failed order_id=%s symbol=%s side=%s", order_id, symbol, side)


def _execute_direct_limit_order(symbol: str, side: str, price: float, qty: int):
    try:
        side_u = str(side or "").upper()
        side_code = 2 if side_u == "BUY" else 1
        px = float(price)
        q = int(qty)
        if px <= 0 or q <= 0:
            logger.error("[ENTRY DIRECT LIMIT INVALID] symbol=%s side=%s price=%s qty=%s", symbol, side_u, price, qty)
            return None
        entry_exchange = int(getattr(bse, "ENTRY_EXCHANGE", 27))
        logger.warning("[ENTRY DIRECT LIMIT DISPATCH] symbol=%s side=%s price=%s qty=%s exchange=%s reason=use_order_builder_limit_price", symbol, side_u, px, q, entry_exchange)
        payload = bse._make_payload(symbol, side=side_code, qty=q, price=px, exchange=entry_exchange, front_order_type=20)
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

def place_entry_buy(symbol: str, symbolname: str, price: Optional[float], reason: str, *args, order_type: str = "LIMIT", qty: Optional[int] = None):
    if args:
        if len(args) >= 1:
            order_type = args[0]
        if len(args) >= 2:
            qty = args[1]
    p = _normalize_args(symbol, symbolname, price, reason, order_type, qty)
    logger.info("[ENTRY BUY TRY] symbol=%s type=%s price=%s qty=%s reason=%s", p["symbol"], p["order_type"], p["price"], p["qty"], p["reason"])
    try:
        if not p["symbol"]:
            raise ValueError("BUY requires symbol")

        liq_ok, liq_reason, liq_detail = _final_recent_liquidity_ok(p["symbol"], "BUY")
        if not liq_ok:
            logger.warning("[ENTRY FINAL LIQ GUARD] BUY blocked symbol=%s reason=%s detail=%s", p["symbol"], liq_reason, liq_detail)
            return None

        if p["order_type"] == "MARKET":
            if p["qty"] is None:
                raise ValueError("MARKET BUY requires qty")
            logger.info("[ENTRY BUY DISPATCH] MARKET symbol=%s qty=%s reference_price=%s", p["symbol"], p["qty"], p["price"])
            res = execute_buy_market(p["symbol"], p["qty"], reference_price=p["price"])
        elif p["order_type"] == "STOP":
            if p["price"] is None or p["qty"] is None:
                raise ValueError("STOP BUY requires price & qty")
            logger.info("[ENTRY BUY DISPATCH] STOP symbol=%s stop_price=%s qty=%s", p["symbol"], p["price"], p["qty"])
            res = execute_buy_stop(p["symbol"], p["qty"], p["price"])
        else:
            if p["qty"] is None:
                raise ValueError("LIMIT BUY requires qty")
            if p["price"] is not None:
                logger.info("[ENTRY BUY DISPATCH] LIMIT symbol=%s price=%s qty=%s direct_limit=True", p["symbol"], p["price"], p["qty"])
                res = _execute_direct_limit_order(p["symbol"], "BUY", p["price"], p["qty"])
            else:
                logger.info("[ENTRY BUY DISPATCH] LIMIT symbol=%s qty=%s best_ask_fallback=True", p["symbol"], p["qty"])
                res = execute_buy_at_best_ask(p["symbol"], p["qty"])
        order_id = _normalize_result(res)
        if not order_id:
            logger.error("[ENTRY BUY FAILED] symbol=%s type=%s qty=%s reason=%s", p["symbol"], p["order_type"], p["qty"], p["reason"])
            return None
        _ensure_entry_inflight_set()
        _register_cancel_watch(order_id, p["symbol"], "BUY", p["qty"], p["price"], "ENTRY_BUY")
        logger.info("[ENTRY BUY SENT] symbol=%s type=%s oid=%s qty=%s reason=%s", p["symbol"], p["order_type"], order_id, p["qty"], p["reason"])
        return order_id
    except Exception:
        logger.exception("[ENTRY BUY EXCEPTION] %s", symbol)
        return None


# ============================================================
# SELL
# ============================================================

def place_entry_sell(symbol: str, symbolname: str, price: Optional[float], reason: str, *args, order_type: str = "LIMIT", qty: Optional[int] = None):
    if args:
        if len(args) >= 1:
            order_type = args[0]
        if len(args) >= 2:
            qty = args[1]
    p = _normalize_args(symbol, symbolname, price, reason, order_type, qty)
    logger.info("[ENTRY SELL TRY] symbol=%s type=%s price=%s qty=%s reason=%s", p["symbol"], p["order_type"], p["price"], p["qty"], p["reason"])
    try:
        if not p["symbol"]:
            raise ValueError("SELL requires symbol")

        liq_ok, liq_reason, liq_detail = _final_recent_liquidity_ok(p["symbol"], "SELL")
        if not liq_ok:
            logger.warning("[ENTRY FINAL LIQ GUARD] SELL blocked symbol=%s reason=%s detail=%s", p["symbol"], liq_reason, liq_detail)
            return None

        if p["order_type"] == "MARKET":
            if p["qty"] is None:
                raise ValueError("MARKET SELL requires qty")
            logger.info("[ENTRY SELL DISPATCH] MARKET symbol=%s qty=%s reference_price=%s", p["symbol"], p["qty"], p["price"])
            res = execute_sell_market(p["symbol"], p["qty"], reference_price=p["price"])
        elif p["order_type"] == "STOP":
            if p["price"] is None or p["qty"] is None:
                raise ValueError("STOP SELL requires price & qty")
            logger.info("[ENTRY SELL DISPATCH] STOP symbol=%s stop_price=%s qty=%s", p["symbol"], p["price"], p["qty"])
            res = execute_short_stop(p["symbol"], p["qty"], p["price"])
        else:
            if p["qty"] is None:
                raise ValueError("LIMIT SELL requires qty")
            if p["price"] is not None:
                logger.info("[ENTRY SELL DISPATCH] LIMIT symbol=%s price=%s qty=%s direct_limit=True", p["symbol"], p["price"], p["qty"])
                res = _execute_direct_limit_order(p["symbol"], "SELL", p["price"], p["qty"])
            else:
                logger.info("[ENTRY SELL DISPATCH] LIMIT symbol=%s qty=%s best_bid_fallback=True", p["symbol"], p["qty"])
                res = execute_short_at_best_bid(p["symbol"], p["qty"])
        order_id = _normalize_result(res)
        if not order_id:
            logger.error("[ENTRY SELL FAILED] symbol=%s type=%s qty=%s reason=%s", p["symbol"], p["order_type"], p["qty"], p["reason"])
            return None
        _ensure_entry_inflight_set()
        _register_cancel_watch(order_id, p["symbol"], "SELL", p["qty"], p["price"], "ENTRY_SELL")
        logger.info("[ENTRY SELL SENT] symbol=%s type=%s oid=%s qty=%s reason=%s", p["symbol"], p["order_type"], order_id, p["qty"], p["reason"])
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
    return None


__all__ = ["place_entry_buy", "place_entry_sell", "_unlock_entry"]
