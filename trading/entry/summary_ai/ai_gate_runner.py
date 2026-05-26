# ============================================================
# File   : trading/entry/summary_ai/ai_gate_runner.py
# Version: PRODUCTION-STABLE-REV3.4-DIRECT-BUY-RECENT-VOLUME-GUARD
# ------------------------------------------------------------
# Purpose:
#   - summary候補 DataFrame を AI gate に通す
#   - BUY / SELL の side を明示して AI に渡す
#   - row側に side / ai_side がある場合は行ごとに BUY/SELL を切り替える
#   - AI判定後、BUY/SELL別にTOP20 + AI可否結果をコンソールログへ表示する
#
# REV3.4:
#   - direct place_entry_buy ルートでも直近1分足の出来高を必須確認する
#   - 最新1分足 volume=0 は即NG
#   - 直近N本合計 volume / turnover が閾値未満ならNG
#   - DBが読めない場合は、既定で fail-closed にして薄商い銘柄への誤発注を防ぐ
#   - 既存の market_open / price guard も維持
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .row_adapter import convert_summary_row_to_ai_gate_row
from .utils import get_ai_final_entry_check, is_market_open, safe_df, safe_float, safe_str

logger = logging.getLogger(__name__)

DEFAULT_MIN_AI_CONFIDENCE = 0.65
DEFAULT_CONSOLE_TOP_N = 20


def _append_reason(base: str, extra: str) -> str:
    base = safe_str(base, "")
    extra = safe_str(extra, "")
    if not extra:
        return base
    if not base:
        return extra
    return f"{base}|{extra}"


def _safe_bool(v: Any, default: bool = False) -> bool:
    try:
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "y", "ok"}:
            return True
        if s in {"0", "false", "no", "off", "n", "ng", ""}:
            return False
        return default
    except Exception:
        return default


def _env_bool(name: str, default: bool = True) -> bool:
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


def _side_value(side: Any) -> str:
    s = str(side or "BUY").strip().upper()
    if s not in {"BUY", "SELL"}:
        s = "BUY"
    return s


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _first_existing(row: pd.Series, names: list[str], default: Any = None) -> Any:
    for c in names:
        try:
            if c in row.index:
                v = row.get(c)
                if v is None:
                    continue
                if isinstance(v, str) and v.strip() == "":
                    continue
                return v
        except Exception:
            pass
    return default


def _row_side(row: pd.Series, default_side: str) -> str:
    for c in ("ai_side", "side", "entry_decision", "signal"):
        try:
            if c in row.index:
                v = str(row.get(c) or "").strip().upper()
                if v in {"BUY", "SELL"}:
                    return v
        except Exception:
            pass

    buy_score = safe_float(_first_existing(row, ["ai_disp_buy_score", "disp_buy_score", "score_buy", "buy_score"], 0.0), 0.0)
    sell_score = safe_float(_first_existing(row, ["ai_disp_sell_score", "disp_sell_score", "score_sell", "sell_score"], 0.0), 0.0)
    score = safe_float(_first_existing(row, ["score_total", "total_score", "final_score", "display_score", "score"], 0.0), 0.0)

    if sell_score > buy_score and sell_score > 0:
        return "SELL"
    if buy_score > sell_score and buy_score > 0:
        return "BUY"
    if score < 0:
        return "SELL"
    if score > 0:
        return "BUY"
    return _side_value(default_side)


def _get_place_entry_buy():
    try:
        from trading.handlers.entry_handler import place_entry_buy
        return place_entry_buy
    except Exception:
        logger.exception("[SUMMARY AI ENTRY] failed to import place_entry_buy")
        return None


def _entry_price_bounds() -> tuple[float, float, dict[str, Any]]:
    min_price = 0.0
    max_price = 0.0
    diag: dict[str, Any] = {"source": "none"}
    try:
        from trading.entry.entry_budget import (
            get_entry_min_price,
            get_entry_max_price,
            get_effective_entry_max_price,
            get_max_entry_oneshot_yen,
            get_order_lot_size,
        )
        min_price = float(get_entry_min_price())
        max_price = float(get_effective_entry_max_price() or get_entry_max_price() or 0.0)
        diag = {
            "source": "entry_budget",
            "entry_min_price": min_price,
            "entry_max_price_effective": max_price,
            "max_oneshot_yen": float(get_max_entry_oneshot_yen()),
            "lot_size": int(get_order_lot_size()),
        }
    except Exception:
        logger.debug("[SUMMARY AI ENTRY] entry budget bounds unavailable", exc_info=True)
    return max(0.0, min_price), max(0.0, max_price), diag


def _price_allowed_for_direct_entry(symbol: str, price: float) -> tuple[bool, str, dict[str, Any]]:
    min_price, max_price, diag = _entry_price_bounds()
    if price > 0 and min_price > 0 and price < min_price:
        return False, f"price_below_entry_min_price:{price:.1f}<{min_price:.1f}", diag
    if price > 0 and max_price > 0 and price > max_price:
        return False, f"price_over_entry_max_price:{price:.1f}>{max_price:.1f}", diag
    return True, "ok", diag


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


def _recent_volume_values(symbol: str, *, bars: int) -> dict[str, Any]:
    path = _summary_db_path()
    table = os.getenv("SUMMARY_AI_DIRECT_LIQ_TABLE", os.getenv("SUMMARY_AI_LIQ_SUMMARY_TABLE", "stock_summary_1min"))
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
            select = f'{tm}, {cl}, {vo}, {tv or "0"}'
            rows = conn.execute(
                f'SELECT {select} FROM "{table}" WHERE CAST({sym} AS TEXT)=? ORDER BY {tm} DESC LIMIT ?',
                (_norm_symbol(symbol), max(1, int(bars))),
            ).fetchall()
        if not rows:
            return {"ok_read": True, "rows": 0, "reason": "no_recent_rows", "summary_db": path, "table": table}
        latest_close = safe_float(rows[0][1], 0.0)
        latest_volume = safe_float(rows[0][2], 0.0)
        latest_turnover_raw = safe_float(rows[0][3], 0.0)
        volumes = [max(0.0, safe_float(r[2], 0.0)) for r in rows]
        closes = [max(0.0, safe_float(r[1], 0.0)) for r in rows]
        turnovers_raw = [max(0.0, safe_float(r[3], 0.0)) for r in rows]
        volume_sum = float(sum(volumes))
        turnover_sum_raw = float(sum(turnovers_raw))
        turnover_calc = float(sum(c * v for c, v in zip(closes, volumes) if c > 0 and v > 0))
        turnover_sum = max(turnover_sum_raw, turnover_calc)
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
            "turnover_sum": turnover_sum,
            "turnover_sum_raw": turnover_sum_raw,
            "turnover_calc": turnover_calc,
        }
    except Exception as e:
        logger.debug("[SUMMARY AI ENTRY] direct recent volume read failed symbol=%s", symbol, exc_info=True)
        return {"ok_read": False, "reason": "exception", "error": str(e), "summary_db": path, "table": table}


def _recent_liquidity_allowed_for_direct_entry(symbol: str) -> tuple[bool, str, dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_DIRECT_LIQ_GUARD_ENABLED", True):
        return True, "disabled", {}
    bars = max(1, _env_int("SUMMARY_AI_DIRECT_LIQ_RECENT_BARS", _env_int("SUMMARY_AI_LIQ_RECENT_BARS", 5)))
    min_latest_volume = _env_float("SUMMARY_AI_DIRECT_LIQ_MIN_LATEST_VOLUME", 1.0)
    min_volume = _env_float("SUMMARY_AI_DIRECT_LIQ_MIN_VOLUME", _env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("SUMMARY_AI_DIRECT_LIQ_MIN_TURNOVER_YEN", _env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 10000000.0))
    require_data = _env_bool("SUMMARY_AI_DIRECT_LIQ_REQUIRE_DATA", True)
    v = _recent_volume_values(symbol, bars=bars)
    detail = {"symbol": symbol, "bars": bars, "min_latest_volume": min_latest_volume, "min_volume": min_volume, "min_turnover": min_turnover, "require_data": require_data, **v}
    if not bool(v.get("ok_read")):
        return (False, f"recent_liquidity_read_ng:{v.get('reason')}", detail) if require_data else (True, "recent_liquidity_read_fail_open", detail)
    if int(v.get("rows") or 0) <= 0:
        return False, "recent_liquidity_no_rows", detail
    if safe_float(v.get("latest_volume"), 0.0) < min_latest_volume:
        return False, f"latest_volume_low:{safe_float(v.get('latest_volume'), 0.0):.0f}<{min_latest_volume:.0f}", detail
    if safe_float(v.get("volume_sum"), 0.0) < min_volume:
        return False, f"recent_volume_low:{safe_float(v.get('volume_sum'), 0.0):.0f}<{min_volume:.0f}", detail
    if safe_float(v.get("turnover_sum"), 0.0) < min_turnover:
        return False, f"recent_turnover_low:{safe_float(v.get('turnover_sum'), 0.0):.0f}<{min_turnover:.0f}", detail
    return True, "ok", detail


def _inject_daily_fields_to_ai_row(ai_row: Dict[str, Any], row: pd.Series) -> Dict[str, Any]:
    for c in (
        "daily_score",
        "daily_buy_score",
        "daily_sell_score",
        "daily_ok_buy",
        "daily_ok_sell",
        "daily_exit_warn",
        "daily_reason",
        "daily_date",
    ):
        if c not in row.index:
            continue
        try:
            v = row.get(c)
            if c in {"daily_ok_buy", "daily_ok_sell", "daily_exit_warn"}:
                ai_row[c] = _safe_bool(v, False)
            elif c in {"daily_score", "daily_buy_score", "daily_sell_score"}:
                ai_row[c] = safe_float(v, 0.0)
            else:
                ai_row[c] = safe_str(v, "")
        except Exception:
            pass
    ai_row["daily_trend_score"] = safe_float(ai_row.get("daily_score"), 0.0)
    ai_row["daily_trend_ok"] = _safe_bool(ai_row.get("daily_ok_buy"), False)
    ai_row["daily_exit_risk"] = _safe_bool(ai_row.get("daily_exit_warn"), False)
    return ai_row


def _sort_for_console(items: list[dict[str, Any]], side: str) -> list[dict[str, Any]]:
    side_s = _side_value(side)
    rows = [x for x in items if str(x.get("side") or x.get("ai_side") or "").upper() == side_s]
    def key_buy(x: dict[str, Any]):
        return (safe_float(x.get("buy_score")), safe_float(x.get("score_total")), safe_float(x.get("final_score")), safe_float(x.get("confidence")))
    def key_sell(x: dict[str, Any]):
        return (safe_float(x.get("sell_score")), -safe_float(x.get("score_total")), -safe_float(x.get("final_score")), safe_float(x.get("confidence")))
    return sorted(rows, key=key_buy if side_s == "BUY" else key_sell, reverse=True)


def _print_summary_ai_top20_console(results: list[dict[str, Any]], *, interval: int | str, source: str, top_n: int = DEFAULT_CONSOLE_TOP_N) -> None:
    try:
        if not results:
            logger.warning("[SUMMARY AI TOP20 RESULT] empty interval=%s source=%s", interval, source)
            return
        for side in ("BUY", "SELL"):
            rows = _sort_for_console(results, side)[: int(top_n)]
            if not rows:
                logger.warning("\n========== SUMMARY AI %s TOP20 RESULT interval=%s source=%s rows=0 ==========", side, interval, source)
                continue
            logger.warning("\n========== SUMMARY AI %s TOP20 RESULT interval=%s source=%s rows=%s ==========", side, interval, source, len(rows))
            for i, r in enumerate(rows, start=1):
                status = "AI_OK" if bool(r.get("allow")) else "AI_NG"
                logger.warning(
                    "%2d. %-5s %-18s %s conf=%.3f C=%.1f buy=%.2f sell=%.2f total=%.2f final=%.2f model=%s reason=%s",
                    i,
                    safe_str(r.get("symbol"), ""),
                    safe_str(r.get("symbolname"), "")[:18],
                    status,
                    safe_float(r.get("confidence"), 0.0),
                    safe_float(r.get("close_price"), 0.0),
                    safe_float(r.get("buy_score"), 0.0),
                    safe_float(r.get("sell_score"), 0.0),
                    safe_float(r.get("score_total"), 0.0),
                    safe_float(r.get("final_score"), 0.0),
                    safe_str(r.get("model_used"), ""),
                    safe_str(r.get("reason"), ""),
                )
            logger.warning("========== END SUMMARY AI %s TOP20 RESULT interval=%s source=%s ==========\n", side, interval, source)
    except Exception:
        logger.exception("[SUMMARY AI TOP20 RESULT] console print failed interval=%s source=%s", interval, source)


def run_ai_gate_for_candidates(candidates_df: pd.DataFrame, *, interval: int | str = 1, source: str = "SUMMARY", min_ai_confidence: float = DEFAULT_MIN_AI_CONFIDENCE, default_dominant_ratio: float = 1.0, side: str = "BUY", use_daily_cache: bool = True, daily_filter_buy: bool = False, daily_hard_block_exit_warn: bool = False, daily_min_score: Optional[float] = None) -> List[Dict[str, Any]]:
    df = safe_df(candidates_df)
    if df.empty:
        logger.warning("[SUMMARY AI GATE] skipped empty candidates interval=%s source=%s side=%s", interval, source, side)
        return []
    default_side = _side_value(side)
    ai_check = get_ai_final_entry_check()
    if ai_check is None:
        logger.error("[SUMMARY AI GATE] ai_final_entry_check not found side=%s", default_side)
        return []
    try:
        side_counts = df.get("ai_side", df.get("side", pd.Series([default_side] * len(df)))).astype(str).str.upper().value_counts().to_dict()
    except Exception:
        side_counts = {default_side: len(df)}
    logger.warning("[SUMMARY AI GATE] SEND_TO_AI start default_side=%s rows=%s side_counts=%s interval=%s source=%s min_conf=%.2f symbols=%s", default_side, len(df), side_counts, interval, source, float(min_ai_confidence), list(df["symbol"].astype(str).head(40)) if "symbol" in df.columns else [])
    results: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        row_side = _row_side(row, default_side)
        ai_row = convert_summary_row_to_ai_gate_row(row, interval=interval, source=source, default_dominant_ratio=default_dominant_ratio, side=row_side)
        ai_row["side"] = row_side
        ai_row["ai_side"] = row_side
        ai_row = _inject_daily_fields_to_ai_row(ai_row, row)
        symbol = safe_str(ai_row.get("symbol"), "")
        symbolname = safe_str(ai_row.get("symbolname"), "")
        try:
            gate_result = ai_check(ai_row)
            if not isinstance(gate_result, dict):
                gate_result = {"allow": False, "confidence": 0.0, "reason": "invalid_ai_result", "model_used": "UNKNOWN"}
        except Exception:
            logger.exception("[SUMMARY AI GATE] AI gate failed side=%s symbol=%s", row_side, symbol)
            gate_result = {"allow": False, "confidence": 0.0, "reason": "ai_gate_exception", "model_used": "ERROR"}
        allow = bool(gate_result.get("allow", False))
        conf = safe_float(gate_result.get("confidence"), 0.0)
        reason = safe_str(gate_result.get("reason"), "")
        model_used = safe_str(gate_result.get("model_used"), "")
        if allow and conf < float(min_ai_confidence):
            allow = False
            reason = _append_reason(reason, f"confidence_low:{conf:.3f}<{float(min_ai_confidence):.3f}")
        if allow and row_side == "BUY" and daily_hard_block_exit_warn and _safe_bool(ai_row.get("daily_exit_warn"), False):
            allow = False
            reason = _append_reason(reason, "daily_exit_warn")
        if allow and daily_min_score is not None:
            try:
                min_score = float(daily_min_score)
                if safe_float(ai_row.get("daily_score"), 0.0) < min_score:
                    allow = False
                    reason = _append_reason(reason, f"daily_score_low:{safe_float(ai_row.get('daily_score'), 0.0):.2f}<{min_score:.2f}")
            except Exception:
                pass
        item = {
            "allow": allow, "confidence": conf, "reason": reason, "model_used": model_used,
            "lot_multiplier": safe_float(gate_result.get("lot_multiplier"), 1.0),
            "side": row_side, "ai_side": row_side, "ai_row": ai_row, "source_row": dict(row),
            "symbol": symbol, "symbolname": symbolname,
            "buy_score": ai_row.get("buy_score"), "sell_score": ai_row.get("sell_score"),
            "score_total": ai_row.get("score_total"), "final_score": ai_row.get("final_score"),
            "close_price": ai_row.get("close_price"), "turnover": ai_row.get("turnover"),
            "daily_score": safe_float(ai_row.get("daily_score"), 0.0),
            "daily_buy_score": safe_float(ai_row.get("daily_buy_score"), 0.0),
            "daily_sell_score": safe_float(ai_row.get("daily_sell_score"), 0.0),
            "daily_ok_buy": _safe_bool(ai_row.get("daily_ok_buy"), False),
            "daily_ok_sell": _safe_bool(ai_row.get("daily_ok_sell"), False),
            "daily_exit_warn": _safe_bool(ai_row.get("daily_exit_warn"), False),
            "daily_reason": safe_str(ai_row.get("daily_reason"), ""),
            "daily_date": safe_str(ai_row.get("daily_date"), ""),
        }
        results.append(item)
        logger.info("[SUMMARY AI GATE] AI_%s side=%s symbol=%s name=%s conf=%.3f buy=%.2f sell=%.2f total=%.2f close=%.1f reason=%s model=%s", "OK" if allow else "NG", row_side, symbol, symbolname, conf, safe_float(ai_row.get("buy_score")), safe_float(ai_row.get("sell_score")), safe_float(ai_row.get("score_total")), safe_float(ai_row.get("close_price")), reason, model_used)
    buy_sent = len([x for x in results if str(x.get("side")).upper() == "BUY"])
    sell_sent = len([x for x in results if str(x.get("side")).upper() == "SELL"])
    buy_ok = len([x for x in results if str(x.get("side")).upper() == "BUY" and bool(x.get("allow"))])
    sell_ok = len([x for x in results if str(x.get("side")).upper() == "SELL" and bool(x.get("allow"))])
    logger.warning("[SUMMARY AI GATE] SEND_TO_AI done sent=%s buy_sent=%s sell_sent=%s buy_ok=%s sell_ok=%s interval=%s source=%s", len(results), buy_sent, sell_sent, buy_ok, sell_ok, interval, source)
    _print_summary_ai_top20_console(results, interval=interval, source=source, top_n=DEFAULT_CONSOLE_TOP_N)
    return results


def _extract_entry_values(r: Dict[str, Any]) -> Dict[str, Any]:
    ai_row = r.get("ai_row") or {}
    source_row = r.get("source_row") or {}
    symbol = ai_row.get("symbol") or r.get("symbol") or source_row.get("symbol") or ""
    symbolname = ai_row.get("symbolname") or r.get("symbolname") or source_row.get("symbolname") or ""
    price = ai_row.get("close_price") or ai_row.get("close") or r.get("close_price") or source_row.get("close")
    reason = r.get("reason") or "AI_OK"
    return {"symbol": str(symbol).strip(), "symbolname": str(symbolname), "price": safe_float(price, 0.0), "reason": str(reason)}


def run_push_summary_ai_entry(summary_df: Optional[pd.DataFrame] = None, *, df: Optional[pd.DataFrame] = None, interval: int | str = 1, interval_label: Optional[str] = None, source: str = "SUMMARY", top_n: int = 20, max_entries: int = 1, min_ai_confidence: float = DEFAULT_MIN_AI_CONFIDENCE, min_confidence: Optional[float] = None, min_conf: Optional[float] = None, dry_run: bool = False, require_market_open: bool = True, default_dominant_ratio: float = 1.0, order_type: Optional[str] = None, test_qty: Optional[int] = None, side: str = "BUY", **kwargs) -> Dict[str, Any]:
    base_df = summary_df if isinstance(summary_df, pd.DataFrame) else df
    base_df = safe_df(base_df)
    if min_confidence is not None:
        min_ai_confidence = float(min_confidence)
    if min_conf is not None:
        min_ai_confidence = float(min_conf)
    try:
        top_n = int(top_n)
    except Exception:
        top_n = 20
    if top_n <= 0:
        top_n = 20
    side_s = _side_value(side)
    logger.info("[SUMMARY AI ENTRY] received rows=%s interval=%s source=%s side=%s top_n=%s max_entries=%s dry_run=%s require_market_open=%s", len(base_df), interval, source, side_s, top_n, max_entries, dry_run, require_market_open)
    if base_df.empty:
        return {"candidates": [], "ai_results": [], "ai_ok": [], "approved_rows": [], "execution": {"executed": False, "orders": [], "skip_reason": "empty_df"}}
    candidates_df = base_df.head(top_n).copy()
    ai_results = run_ai_gate_for_candidates(candidates_df, interval=interval, source=source, min_ai_confidence=float(min_ai_confidence), default_dominant_ratio=default_dominant_ratio, side=side_s)
    ai_ok = [r for r in ai_results if bool(r.get("allow"))]
    if side_s != "BUY":
        logger.warning("[SUMMARY AI ENTRY] side=%s evaluated only; real BUY entry skipped ai_ok=%s", side_s, len(ai_ok))
        return {"candidates": candidates_df, "ai_results": ai_results, "ai_ok": ai_ok, "approved_rows": [], "execution": {"executed": False, "orders": [], "skip_reason": "non_buy_side_evaluated_only"}}
    if require_market_open and not is_market_open():
        logger.warning("[SUMMARY AI ENTRY] market closed; direct BUY entry skipped interval=%s source=%s ai_ok=%s symbols=%s", interval, source, len(ai_ok), [str(x.get("symbol")) for x in ai_ok[:30]])
        return {"candidates": candidates_df, "ai_results": ai_results, "ai_ok": ai_ok, "approved_rows": [], "execution": {"executed": False, "orders": [], "skip_reason": "market_closed"}}
    place_entry_buy = _get_place_entry_buy()
    if place_entry_buy is None:
        return {"candidates": candidates_df, "ai_results": ai_results, "ai_ok": ai_ok, "approved_rows": ai_ok, "execution": {"executed": False, "orders": [], "skip_reason": "place_entry_buy_import_failed"}}
    orders: List[Dict[str, Any]] = []
    try:
        max_entries_i = max(1, int(max_entries))
    except Exception:
        max_entries_i = 1
    approved_rows: List[Dict[str, Any]] = []
    for r in ai_ok:
        if str(r.get("side", "BUY")).upper() != "BUY":
            continue
        v = _extract_entry_values(r)
        if not v["symbol"]:
            continue
        price_ok, price_reason, price_diag = _price_allowed_for_direct_entry(v["symbol"], v["price"])
        if not price_ok:
            logger.warning("[SUMMARY AI ENTRY] direct BUY skipped by price guard symbol=%s price=%.1f reason=%s diag=%s", v["symbol"], v["price"], price_reason, price_diag)
            continue
        liq_ok, liq_reason, liq_diag = _recent_liquidity_allowed_for_direct_entry(v["symbol"])
        if not liq_ok:
            logger.warning("[SUMMARY AI ENTRY] direct BUY skipped by recent liquidity guard symbol=%s price=%.1f reason=%s diag=%s", v["symbol"], v["price"], liq_reason, liq_diag)
            continue
        approved_rows.append(r)
        if len(approved_rows) >= max_entries_i:
            break
    if not approved_rows:
        logger.warning("[SUMMARY AI ENTRY] no direct BUY rows after side/price/recent-liquidity guard ai_ok=%s", len(ai_ok))
        return {"candidates": candidates_df, "ai_results": ai_results, "ai_ok": ai_ok, "approved_rows": [], "execution": {"executed": False, "orders": [], "skip_reason": "no_buy_after_direct_guard"}}
    for r in approved_rows:
        v = _extract_entry_values(r)
        if dry_run:
            logger.warning("[SUMMARY AI ENTRY DRY_RUN] would entry BUY symbol=%s price=%.1f reason=%s", v["symbol"], v["price"], v["reason"])
            orders.append({"symbol": v["symbol"], "ok": True, "dry_run": True, "order_id": None})
            continue
        try:
            order_id = place_entry_buy(v["symbol"], v["symbolname"], v["price"], v["reason"], order_type=str(order_type or "LIMIT"), qty=int(test_qty or 100))
            orders.append({"symbol": v["symbol"], "ok": bool(order_id), "dry_run": False, "order_id": order_id})
        except Exception:
            logger.exception("[SUMMARY AI ENTRY SEND FAILED] symbol=%s", v["symbol"])
            orders.append({"symbol": v["symbol"], "ok": False, "dry_run": False, "order_id": None})
    executed = any(bool(x.get("ok")) for x in orders)
    return {"candidates": candidates_df, "ai_results": ai_results, "ai_ok": ai_ok, "approved_rows": approved_rows, "execution": {"executed": executed, "orders": orders, "skip_reason": None if executed else "no_entry_sent"}}


def run_summary_ai_entry_from_df(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run_summary_ai_gate(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run_ai_gate_once(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def start(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


__all__ = [
    "DEFAULT_MIN_AI_CONFIDENCE",
    "run_ai_gate_for_candidates",
    "run_push_summary_ai_entry",
    "run_summary_ai_entry_from_df",
    "run_summary_ai_gate",
    "run_ai_gate_once",
    "run",
    "start",
]
