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
from sqlite_cm import committing_connect
from nas_root import nas_path, nas_root

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
    base = os.getenv("SUMMARY_DB_DIR", nas_path(r"raw_data\kabu_station\summary"))
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


def _recent_liquidity_values_local(symbol: str, bars: int) -> dict[str, Any] | None:
    """ローカル push DB の 1 分足 (trading/summary/mtf_local_bars) から直近 bars 本の出来高・売買代金。
    2026-09-17: NAS の summary DB を CAST(symbol AS TEXT)=? で全表走査していて 1 銘柄 2 分かかり、
    さらに列検出が空で required_columns_missing になって **全発注が止まっていた** (5574, 3907)。
    ローカルに足があればそれで判定し、無いときだけ NAS へ落ちる。"""
    try:
        from trading.summary.mtf_local_bars import get_bars, is_stale
        b = get_bars(symbol, 1)
        if b is None or b.empty or is_stale(b, 1, max_age_bars=3.0):
            return None
        t = b.tail(max(1, int(bars)))
        closes = [max(0.0, _f(x, 0.0)) for x in t["close"].tolist()]
        volumes = [max(0.0, _f(x, 0.0)) for x in t["volume"].tolist()]
        if not closes:
            return None
        latest_close, latest_volume = closes[-1], volumes[-1]
        volume_sum = float(sum(volumes))
        turnover_calc = float(sum(c * v for c, v in zip(closes, volumes) if c > 0 and v > 0))
        return {
            "ok_read": True,
            "rows": len(t),
            "summary_db": "local_push_1m",
            "table": "mtf_local_bars",
            "latest_dt": str(t["datetime"].iloc[-1]),
            "latest_close": latest_close,
            "latest_volume": latest_volume,
            "latest_turnover": latest_close * latest_volume,
            "volume_sum": volume_sum,
            "turnover_sum": turnover_calc,
            "turnover_raw_sum": 0.0,
            "turnover_calc": turnover_calc,
        }
    except Exception:
        logger.debug("[ENTRY FINAL LIQ GUARD] local read failed symbol=%s", symbol, exc_info=True)
        return None


def _recent_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    if symbol and _env_bool("ENTRY_HANDLER_RECENT_LIQ_LOCAL_FIRST", True):
        loc = _recent_liquidity_values_local(symbol, bars)
        if loc is not None:
            return loc
        if not _env_bool("ENTRY_HANDLER_RECENT_LIQ_NAS_FALLBACK", False):
            # ローカルに足が無い = PUSH 未登録 (板も取れない)。NAS を 2 分かけて読みに行かない。
            return {"ok_read": False, "reason": "local_bars_missing", "summary_db": "local_push_1m"}
    path = _summary_db_path()
    table = os.getenv("ENTRY_HANDLER_RECENT_LIQ_TABLE", os.getenv("SUMMARY_AI_LIQ_SUMMARY_TABLE", "stock_summary_1min"))
    if not symbol or not Path(path).exists():
        return {"ok_read": False, "reason": "summary_db_missing", "summary_db": path, "table": table}
    try:
        with committing_connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            sym = _col(conn, table, ["symbol", "code", "stock_code"])
            tm = _col(conn, table, ["datetime", "dt", "timestamp", "time"])
            cl = _col(conn, table, ["close_price", "close", "price", "current_price"])
            vo = _col(conn, table, ["volume", "Volume", "vol", "出来高"])
            tv = _col(conn, table, ["turnover_yen", "turnover", "trading_value", "売買代金"])
            if not sym or not tm or not cl or not vo:
                return {"ok_read": False, "reason": "required_columns_missing", "summary_db": path, "table": table, "sym_col": sym, "tm_col": tm, "close_col": cl, "volume_col": vo}
            rows = conn.execute(
                # CAST(symbol AS TEXT)=? は索引が効かず NAS で全表走査になる (9/14 と同じ罠)
                f'SELECT {tm}, {cl}, {vo}, {tv or "0"} FROM "{table}" WHERE {sym}=? ORDER BY {tm} DESC LIMIT ?',
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


def _final_recent_liquidity_ok_core(symbol: str, side: str) -> tuple[bool, str, dict[str, Any]]:
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
    # 2026-09-17: 株数と売買代金は「どちらか」で足りれば通す。3,000 円台の銘柄で 5 分 3 万株 (約 1 億円) を
    #   要求すると、売買代金 1,300 万円/5 分の 5574 でも落ちてしまう。
    vol_ok = _f(v.get("volume_sum"), 0.0) >= min_volume
    tv_ok = _f(v.get("turnover_sum"), 0.0) >= min_turnover
    if not vol_ok and not tv_ok:
        return False, f"FINAL_LIQ_LOW:vol {_f(v.get('volume_sum'), 0.0):.0f}<{min_volume:.0f} and turnover {_f(v.get('turnover_sum'), 0.0):.0f}<{min_turnover:.0f}", detail
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
        # 2026-10-03: 殿様イナゴ由来の建玉は専用の決済 (損切り 1.5% / 60 分) にする
        try:
            _is_tonosama = "TONOSAMA" in str(p.get("reason") or "").upper()
            # 2026-10-13: 「同じ銘柄に殿様の pending がある」だけで記録すると、SUMMARY_AI の注文まで
            #   殿様扱いになる (10/8 の 6203)。既定では理由の文字列だけで判定する。
            if not _is_tonosama and os.getenv("TONOSAMA_RECORD_BY_PENDING", "0").strip() == "1":
                from trading.entry.tonosama.pending_writer import has_tonosama_pending as _has_tp
                _is_tonosama = bool(_has_tp(p["symbol"]))
            if _is_tonosama:
                from trading.strategies.tonosama_exit import record_entry as _tonosama_record
                _tonosama_record(p["symbol"], order_id, p["price"], p["qty"], p["reason"])
        except Exception:
            logger.exception("[ENTRY BUY SENT] tonosama record failed symbol=%s", p["symbol"])
        return order_id
    except Exception:
        logger.exception("[ENTRY BUY EXCEPTION] %s", symbol)
        return None


# ============================================================
# SELL
# ============================================================

def _has_sell_wait_intent(symbol: str) -> bool:
    try:
        from trading.handlers.pending_order_monitor import _ORDER_INTENT, _ORDER_INTENT_TTL_SEC
        import time as _t
        it = _ORDER_INTENT.get(str(symbol or "").strip()) or {}
        return it.get("mode") == "sell_wait" and _t.time() - float(it.get("ts") or 0) <= _ORDER_INTENT_TTL_SEC
    except Exception:
        return False


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

        # 2026-10-20: Summary AI の新規売りは止める (entry_controller と同じ env)。別の経路から来ても送らない。
        #   2026-10-20 午後: 売気配の 0.2% 上で待つ注文 (entry_order_builder が mode=sell_wait を置いたもの) だけは通す。
        if (str(p["reason"] or "").upper().startswith("SUMMARY_AI")
                and str(os.getenv("ENTRY_SUMMARY_AI_SELL_ENABLED", "0")).strip().lower() not in ("1", "true", "yes", "on")
                and not (_env_bool("ENTRY_SUMMARY_AI_SELL_WAIT_ENABLED", True) and _has_sell_wait_intent(p["symbol"]))):
            logger.warning("[SUMMARY AI SELL DISABLED] 新規売りを送らない symbol=%s price=%s qty=%s (ENTRY_SUMMARY_AI_SELL_ENABLED=1 で戻す)",
                           p["symbol"], p["price"], p["qty"])
            return None

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


# ============================================================
# _final_recent_liquidity_ok の2層ガード
# ------------------------------------------------------------
# 旧 core/startup の2パッチを本文化。実行時の合成は
#   native <- pending_fallback <- memory
# の2層だった (実測)。
# 3つ目の entry_handler_strict_recent_liquidity_patch は、pending_fallback が
# `getattr(cur, "_original", cur)` で素の実装まで掘り返すため wrapper が
# 捨てられており実効ゼロだったので移植していない。ただし同パッチの
# env 既定値4件は load-bearing なので呼び出し元へ移設済み
# (特に ENTRY_HANDLER_RECENT_LIQ_MIN_LATEST_VOLUME=3000。
#  pending_fallback 側の既定は 1.0 なので、消すとしきい値が落ちる)。
#
# スコープは旧 install 導線 (いずれも main.py 経由) を再現する。
# ============================================================

_PLFVERSION = "V1-ENTRY-FINAL-LIQ-PENDING-FALLBACK"
_MLQVERSION = "V1-STRICT-RECENT-LIQ-MEMORY"
_FRL_SCOPE = None

def _plf_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _plf_norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _plf_f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _plf_parse_dt(v: Any) -> dt.datetime | None:
    try:
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        if v is None or str(v).strip() == "":
            return None
        s = str(v).strip()
        # pandas Timestamp string / ISO-like string
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return dt.datetime.strptime(s, fmt)
            except Exception:
                continue
    except Exception:
        return None
    return None


def _plf_iter_pending_for_symbol(symbol: str):
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", {}) or {}
        sym = _plf_norm_symbol(symbol)
        bucket = root.get(sym) or root.get(str(symbol)) or []
        if isinstance(bucket, dict):
            bucket = [bucket]
        if isinstance(bucket, list):
            for e in reversed(bucket):
                if isinstance(e, dict):
                    yield e
    except Exception:
        return


def _plf_pending_liq_ok(symbol: str, side: str) -> tuple[bool, str, dict[str, Any]]:
    min_latest_volume = _plf_env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_LATEST_VOLUME", 1.0)
    min_volume = _plf_env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_VOLUME", _plf_env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0))
    min_turnover = _plf_env_float("ENTRY_HANDLER_RECENT_LIQ_MIN_TURNOVER_YEN", _plf_env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 1000000.0))
    max_age = _plf_env_float("ENTRY_HANDLER_RECENT_LIQ_MAX_AGE_SEC", _plf_env_float("ENTRY_HANDLER_STRICT_LIQ_MAX_AGE_SEC", 180.0))
    now = dt.datetime.now()
    sym = _plf_norm_symbol(symbol)

    best_detail: dict[str, Any] = {"symbol": sym, "side": side, "source": "pending_entry_row", "min_latest_volume": min_latest_volume, "min_volume": min_volume, "min_turnover": min_turnover, "max_age_sec": max_age}
    for row in _plf_iter_pending_for_symbol(sym):
        row_sym = _plf_norm_symbol(row.get("symbol") or sym)
        if row_sym != sym:
            continue
        row_side = str(row.get("side") or row.get("entry_decision") or row.get("ai_side") or "").upper()
        if row_side in {"BUY", "SELL"} and str(side or "").upper() in {"BUY", "SELL"} and row_side != str(side).upper():
            continue
        ts = None
        for k in ("datetime", "dt", "time", "created_at", "updated_at"):
            ts = _plf_parse_dt(row.get(k))
            if ts is not None:
                break
        age = None if ts is None else max(0.0, (now - ts).total_seconds())
        price = _plf_f(row.get("close_price") or row.get("price") or row.get("current_price") or row.get("close"), 0.0)
        vol = _plf_f(row.get("volume") or row.get("vol") or row.get("latest_volume") or row.get("display_volume"), 0.0)
        turnover = _plf_f(row.get("turnover") or row.get("trading_value") or row.get("turnover_yen") or row.get("display_turnover"), 0.0)
        if turnover <= 0 and price > 0 and vol > 0:
            turnover = price * vol
        detail = dict(best_detail)
        detail.update({"row_datetime": str(ts) if ts else None, "age_sec": age, "price": price, "latest_volume": vol, "volume_sum": vol, "turnover_sum": turnover, "entry_type": row.get("entry_type"), "row_source": row.get("source")})
        if age is None:
            best_detail = detail | {"ng": "pending_row_no_time"}
            continue
        if age > max_age:
            best_detail = detail | {"ng": f"pending_row_stale:{age:.1f}>{max_age:.1f}"}
            continue
        if vol < min_latest_volume:
            best_detail = detail | {"ng": f"pending_latest_volume_low:{vol:.0f}<{min_latest_volume:.0f}"}
            continue
        if vol < min_volume:
            best_detail = detail | {"ng": f"pending_volume_low:{vol:.0f}<{min_volume:.0f}"}
            continue
        if turnover < min_turnover:
            best_detail = detail | {"ng": f"pending_turnover_low:{turnover:.0f}<{min_turnover:.0f}"}
            continue
        logger.warning("[ENTRY FINAL LIQ PENDING FALLBACK] OK symbol=%s side=%s detail=%s version=%s", sym, side, detail, _PLFVERSION)
        return True, "FINAL_LIQ_PENDING_ROW_OK", detail
    return False, "FINAL_LIQ_PENDING_ROW_NG", best_detail


def _mlq_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _mlq_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _mlq_f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _mlq_norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _mlq_parse_dt(v: Any):
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    try:
        if hasattr(v, "to_pydatetime"):
            x = v.to_pydatetime()
            if isinstance(x, dt.datetime):
                return x.replace(tzinfo=None)
    except Exception:
        pass
    try:
        s = str(v).strip()
        if not s or s.lower() in {"nat", "none", "nan"}:
            return None
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _mlq_memory_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    sym = _mlq_norm_symbol(symbol)
    if not sym:
        return {"ok_read": False, "reason": "symbol_missing", "source": "memory_summary"}
    try:
        import pandas as pd
        from core.global_context import context as gc
    except Exception as e:
        return {"ok_read": False, "reason": "import_failed", "error": str(e), "source": "memory_summary"}

    frames: list[tuple[str, Any]] = []
    for src in ("push", "ranking", "legacy", "push-cache"):
        try:
            df = gc.get_summary_history(tf=1, source=src)
            if df is not None and not getattr(df, "empty", True):
                frames.append((f"history:{src}", df))
        except Exception:
            pass
    for src in ("push", "ranking", "legacy", "push-cache"):
        try:
            df = gc.get_merged_summary(tf=1, source=src)
            if df is not None and not getattr(df, "empty", True):
                frames.append((f"merged:{src}", df))
        except Exception:
            pass
    try:
        df = gc.get_push_df()
        if df is not None and not getattr(df, "empty", True):
            frames.append(("push_df", df))
    except Exception:
        pass

    best: dict[str, Any] | None = None
    for source, df in frames:
        try:
            if "symbol" not in df.columns:
                continue
            x = df.copy()
            x["_symbol_norm"] = x["symbol"].map(_mlq_norm_symbol)
            x = x[x["_symbol_norm"] == sym].copy()
            if x.empty:
                continue
            time_col = None
            for c in ("datetime", "end_time", "start_time", "time", "created_at", "updated_at"):
                if c in x.columns:
                    time_col = c
                    break
            if time_col:
                x["_dt"] = pd.to_datetime(x[time_col], errors="coerce").dt.tz_localize(None)
                x = x.sort_values("_dt", ascending=True, na_position="first")
            else:
                x["_dt"] = pd.NaT
            tail = x.tail(max(1, int(bars))).copy()
            vol_col = next((c for c in ("volume", "latest_volume", "display_volume", "vol") if c in tail.columns), None)
            price_col = next((c for c in ("close", "close_price", "price", "current_price") if c in tail.columns), None)
            turnover_col = next((c for c in ("turnover", "trading_value", "display_turnover") if c in tail.columns), None)
            if vol_col is None:
                continue
            vol = pd.to_numeric(tail[vol_col], errors="coerce").fillna(0.0)
            price = pd.to_numeric(tail[price_col], errors="coerce").fillna(0.0) if price_col else pd.Series(0.0, index=tail.index)
            if turnover_col:
                turnover = pd.to_numeric(tail[turnover_col], errors="coerce").fillna(0.0)
            else:
                turnover = price * vol
            if turnover.fillna(0).sum() <= 0 and price.fillna(0).gt(0).any():
                turnover = price * vol
            latest = tail.iloc[-1]
            latest_dt = _mlq_parse_dt(latest.get("_dt"))
            rows = int(len(tail))
            out = {
                "ok_read": True,
                "source": source,
                "rows": rows,
                "latest_dt": latest_dt.isoformat(sep=" ") if latest_dt else None,
                "latest_close": _mlq_f(latest.get(price_col) if price_col else 0.0),
                "latest_volume": float(vol.iloc[-1]) if len(vol) else 0.0,
                "volume_sum": float(vol.sum()),
                "turnover_sum": float(turnover.sum()),
            }
            if best is None:
                best = out
            else:
                bdt = _mlq_parse_dt(best.get("latest_dt"))
                if latest_dt is not None and (bdt is None or latest_dt > bdt):
                    best = out
        except Exception:
            logger.debug("[ENTRY FINAL LIQ MEMORY] source scan failed source=%s symbol=%s", source, sym, exc_info=True)
            continue
    if best is None:
        return {"ok_read": False, "reason": "memory_no_rows", "source": "memory_summary"}
    return best


def _mlq_memory_ok(symbol: str, side: str, original_detail: Any) -> tuple[bool, str, dict[str, Any]]:
    bars = max(1, _mlq_env_int("ENTRY_HANDLER_RECENT_LIQ_BARS", 5))
    min_latest_volume = _mlq_env_float("ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME", _mlq_env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0))
    min_avg_volume = _mlq_env_float("ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME", _mlq_env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0))
    min_turnover = _mlq_env_float("ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN", _mlq_env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1000000.0))
    max_age_sec = _mlq_env_float("ENTRY_HANDLER_STRICT_MAX_AGE_SEC", 180.0)
    values = _mlq_memory_liquidity_values(symbol, bars)
    latest_dt = _mlq_parse_dt(values.get("latest_dt")) if isinstance(values, dict) else None
    age_sec = (dt.datetime.now() - latest_dt).total_seconds() if latest_dt else None
    rows = int(values.get("rows") or 0) if isinstance(values, dict) else 0
    latest_volume = _mlq_f(values.get("latest_volume") if isinstance(values, dict) else 0.0)
    avg_volume = _mlq_f(values.get("volume_sum") if isinstance(values, dict) else 0.0) / max(1, rows)
    turnover_sum = _mlq_f(values.get("turnover_sum") if isinstance(values, dict) else 0.0)
    detail = {
        "symbol": symbol,
        "side": side,
        "bars": bars,
        "rows": rows,
        "latest_volume": latest_volume,
        "avg_volume": avg_volume,
        "turnover_sum": turnover_sum,
        "latest_dt": values.get("latest_dt") if isinstance(values, dict) else None,
        "age_sec": age_sec,
        "min_latest_volume": min_latest_volume,
        "min_avg_volume": min_avg_volume,
        "min_turnover": min_turnover,
        "max_age_sec": max_age_sec,
        "memory_values": values,
        "original_detail": original_detail,
    }
    if not isinstance(values, dict) or not values.get("ok_read"):
        return False, "STRICT_FINAL_LIQ_MEMORY_READ_NG", detail
    if latest_dt is None or age_sec is None or age_sec > max_age_sec:
        return False, f"STRICT_FINAL_LIQ_MEMORY_STALE:{float(age_sec or 0):.0f}>{max_age_sec:.0f}", detail
    if latest_volume < min_latest_volume:
        return False, f"STRICT_FINAL_LIQ_MEMORY_LATEST_VOLUME_LOW:{latest_volume:.0f}<{min_latest_volume:.0f}", detail
    if avg_volume < min_avg_volume:
        return False, f"STRICT_FINAL_LIQ_MEMORY_AVG_VOLUME_LOW:{avg_volume:.0f}<{min_avg_volume:.0f}", detail
    if turnover_sum < min_turnover:
        return False, f"STRICT_FINAL_LIQ_MEMORY_TURNOVER_LOW:{turnover_sum:.0f}<{min_turnover:.0f}", detail
    logger.warning("[ENTRY FINAL LIQ MEMORY] stale DB bypassed by fresh memory source=%s symbol=%s side=%s age=%.1f latest_vol=%.0f avg_vol=%.0f turnover=%.0f version=%s", values.get("source"), symbol, side, float(age_sec), latest_volume, avg_volume, turnover_sum, _MLQVERSION)
    return True, "STRICT_FINAL_LIQ_OK_MEMORY_FRESH", detail


def _frl_scope_enabled() -> bool:
    global _FRL_SCOPE
    if _FRL_SCOPE is not None:
        return _FRL_SCOPE
    enabled = False
    try:
        import sys as _sys

        argv_text = " ".join(str(x) for x in _sys.argv).replace("\\", "/").lower()
        db_names = (
            "main_database.py", "data_collectors_runner.py", "db_prepare_runner.py",
            "push_receiver_runner.py", "yahoo_complement_runner.py",
            "summary_database_runner.py", "ranking_collector_runner.py",
        )
        if not any(n in argv_text for n in db_names):
            enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _FRL_SCOPE = enabled
    return enabled


def _final_recent_liquidity_layer1_pending_fallback(symbol: str, side: str):
    ok, reason, detail = _final_recent_liquidity_ok_core(symbol, side)
    if ok:
        return ok, reason, detail
    reason_s = str(reason or "")
    # Only rescue stale/missing DB reads with fresh pending row.
    rescue_candidates = (
        "STALE" in reason_s
        or "READ_NG" in reason_s
        or "NO_RECENT_ROWS" in reason_s
        or "LATEST_VOLUME_LOW" in reason_s
        or "VOLUME_LOW" in reason_s
        or "TURNOVER_LOW" in reason_s
    )
    p_ok, p_reason, p_detail = _plf_pending_liq_ok(symbol, side)
    merged = dict(detail or {})
    merged["pending_row_check"] = p_detail
    if rescue_candidates and p_ok:
        logger.warning(
            "[ENTRY FINAL LIQ PENDING FALLBACK] rescue symbol=%s side=%s original=%s pending=%s version=%s",
            symbol, side, reason, p_detail, _PLFVERSION,
        )
        return True, p_reason, merged
    return ok, reason, merged


def _final_recent_liquidity_ok(symbol: str, side: str):
    if not _frl_scope_enabled():
        return _final_recent_liquidity_ok_core(symbol, side)

    ok, reason, detail = _final_recent_liquidity_layer1_pending_fallback(symbol, side)
    try:
        if ok:
            return ok, reason, detail
        text = str(reason or "")
        if text.startswith("STRICT_FINAL_LIQ_STALE"):
            mem_ok, mem_reason, mem_detail = _mlq_memory_ok(symbol, side, detail)
            if mem_ok:
                return True, mem_reason, mem_detail
            merged = dict(mem_detail)
            merged["original_reason"] = reason
            return False, reason, merged
    except Exception:
        logger.exception("[ENTRY FINAL LIQ MEMORY] wrapper failed symbol=%s side=%s", symbol, side)
    return ok, reason, detail
