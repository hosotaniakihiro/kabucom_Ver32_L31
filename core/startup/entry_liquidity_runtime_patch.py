# ============================================================
# File   : core/startup/entry_liquidity_runtime_patch.py
# Version: V1.1-RECENT-1MIN-LIQUIDITY-GUARD
# ------------------------------------------------------------
# 直近の1分足N本を優先して、出来高・売買代金・値動きを判定する。
#
# default:
#   ENTRY_LIQ_RECENT_BARS=5
#   ENTRY_LIQ_MIN_VOLUME=30000
#   ENTRY_LIQ_MIN_TURNOVER_YEN=10000000
#   ENTRY_LIQ_MIN_RANGE_PCT=0.0015
#   ENTRY_LIQ_MIN_ATR_PCT=0.0010
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path() -> str:
    base = os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_today()}.db"))


def _first(row: Dict[str, Any], names: list[str]) -> float:
    for n in names:
        x = _f(row.get(n), 0.0)
        if x > 0:
            return x
    return 0.0


def _col(conn: sqlite3.Connection, table: str, names: list[str]) -> str:
    try:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for n in names:
            if n in cols:
                return n
    except Exception:
        return ""
    return ""


def _entry_row_values(row: Dict[str, Any]) -> Dict[str, Any]:
    close = _first(row, ["close_price", "close", "price", "current_price"])
    volume = _first(row, ["volume", "Volume", "vol", "出来高"])
    high = _first(row, ["high_price", "high"])
    low = _first(row, ["low_price", "low"])
    atr = _first(row, ["atr", "atr_1m", "atr_3m", "atr_5m"])
    turnover = _first(row, ["turnover", "turnover_yen", "trading_value", "売買代金"])
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    return {
        "liq_mode": "entry_row_fallback",
        "liq_bars": 1,
        "close": close,
        "volume": volume,
        "turnover": turnover,
        "range_pct": ((high - low) / close) if close > 0 and high >= low and high > 0 and low > 0 else 0.0,
        "atr_pct": (atr / close) if close > 0 and atr > 0 else 0.0,
    }


def _recent_values(symbol: str, bars: int) -> Dict[str, Any]:
    path = _summary_db_path()
    table = os.getenv("ENTRY_LIQ_SUMMARY_TABLE", "stock_summary_1min")
    if not symbol or not Path(path).exists():
        return {}
    try:
        with sqlite3.connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            sym = _col(conn, table, ["symbol", "code", "stock_code"])
            tm = _col(conn, table, ["datetime", "dt", "timestamp", "time"])
            cl = _col(conn, table, ["close_price", "close", "price", "current_price"])
            hi = _col(conn, table, ["high_price", "high"])
            lo = _col(conn, table, ["low_price", "low"])
            vo = _col(conn, table, ["volume", "Volume", "vol"])
            tv = _col(conn, table, ["turnover", "turnover_yen", "trading_value"])
            at = _col(conn, table, ["atr", "atr_1m", "atr_3m", "atr_5m"])
            if not sym or not tm or not cl:
                return {}
            select = f"{tm}, {cl}, {hi or '0'}, {lo or '0'}, {vo or '0'}, {tv or '0'}, {at or '0'}"
            sql = f"SELECT {select} FROM {table} WHERE CAST({sym} AS TEXT)=? ORDER BY {tm} DESC LIMIT ?"
            rows = conn.execute(sql, (_norm_symbol(symbol), max(1, bars))).fetchall()
            if not rows:
                return {}
        close = _f(rows[0][1], 0.0)
        highs = [_f(r[2], 0.0) for r in rows if _f(r[2], 0.0) > 0]
        lows = [_f(r[3], 0.0) for r in rows if _f(r[3], 0.0) > 0]
        volume = sum(max(0.0, _f(r[4], 0.0)) for r in rows)
        turnover = sum(max(0.0, _f(r[5], 0.0)) for r in rows)
        if turnover <= 0:
            turnover = sum(max(0.0, _f(r[1], 0.0)) * max(0.0, _f(r[4], 0.0)) for r in rows)
        atrs = [_f(r[6], 0.0) for r in rows if _f(r[6], 0.0) > 0]
        return {
            "liq_mode": "recent_summary_1min",
            "liq_bars": len(rows),
            "liq_latest_dt": str(rows[0][0]),
            "close": close,
            "volume": volume,
            "turnover": turnover,
            "range_pct": ((max(highs) - min(lows)) / close) if close > 0 and highs and lows else 0.0,
            "atr_pct": ((sum(atrs) / len(atrs)) / close) if close > 0 and atrs else 0.0,
        }
    except Exception:
        logger.debug("[ENTRY LIQ GUARD] recent read failed symbol=%s path=%s", symbol, path, exc_info=True)
        return {}


def _values(row: Dict[str, Any]) -> Dict[str, Any]:
    if _env_bool("ENTRY_LIQ_USE_RECENT_SUMMARY", True):
        v = _recent_values(_norm_symbol(row.get("symbol")), _env_int("ENTRY_LIQ_RECENT_BARS", 5))
        if v:
            return v
    return _entry_row_values(row)


def _check_liquidity(row: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    if not _env_bool("ENTRY_LIQ_GUARD_ENABLED", True):
        return True, "DISABLED", {}
    v = _values(row)
    detail = {
        "symbol": row.get("symbol"),
        "side": row.get("entry_decision") or row.get("side"),
        "source": row.get("source"),
        "interval": row.get("interval"),
        **v,
        "min_volume": _env_float("ENTRY_LIQ_MIN_VOLUME", 30000.0),
        "min_turnover": _env_float("ENTRY_LIQ_MIN_TURNOVER_YEN", 10000000.0),
        "min_range_pct": _env_float("ENTRY_LIQ_MIN_RANGE_PCT", 0.0015),
        "min_atr_pct": _env_float("ENTRY_LIQ_MIN_ATR_PCT", 0.0010),
    }
    close = _f(v.get("close"), 0.0)
    volume = _f(v.get("volume"), 0.0)
    turnover = _f(v.get("turnover"), 0.0)
    range_pct = _f(v.get("range_pct"), 0.0)
    atr_pct = _f(v.get("atr_pct"), 0.0)
    if _env_bool("ENTRY_LIQ_REQUIRE_DATA", True):
        if close <= 0:
            return False, "LIQUIDITY_NO_RECENT_CLOSE", detail
        if volume <= 0:
            return False, "LIQUIDITY_NO_RECENT_VOLUME", detail
    if volume < detail["min_volume"]:
        return False, "LIQUIDITY_RECENT_VOLUME_LOW", detail
    if turnover < detail["min_turnover"]:
        return False, "LIQUIDITY_RECENT_TURNOVER_LOW", detail
    if range_pct < detail["min_range_pct"] and atr_pct < detail["min_atr_pct"]:
        return False, "LIQUIDITY_RECENT_MOVEMENT_LOW", detail
    return True, "LIQUIDITY_OK", detail


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY LIQ GUARD] import failed")
        return False
    old = getattr(ec, "_execute_best_candidate", None)
    if not callable(old):
        logger.warning("[ENTRY LIQ GUARD] _execute_best_candidate not callable")
        return False
    if not getattr(old, "_entry_liq_guard_wrapped_v11", False):
        def wrapped(item: dict, boost_active: bool) -> bool:
            try:
                row = item.get("entry_row") if isinstance(item, dict) else None
                if isinstance(row, dict):
                    row.setdefault("symbol", item.get("symbol"))
                    row.setdefault("side", item.get("side"))
                    ok, reason, detail = _check_liquidity(row)
                    if not ok:
                        ec._log_skip(str(item.get("symbol")), reason, **detail)
                        return False
            except Exception:
                logger.exception("[ENTRY LIQ GUARD] precheck failed")
            return old(item, boost_active=boost_active)
        wrapped._entry_liq_guard_wrapped_v11 = True  # type: ignore[attr-defined]
        wrapped._original = old  # type: ignore[attr-defined]
        ec._execute_best_candidate = wrapped
    _INSTALLED = True
    logger.warning(
        "[ENTRY LIQ GUARD] installed v1.1 recent_bars=%s min_volume=%s min_turnover=%s min_range_pct=%s min_atr_pct=%s",
        _env_int("ENTRY_LIQ_RECENT_BARS", 5),
        _env_float("ENTRY_LIQ_MIN_VOLUME", 30000.0),
        _env_float("ENTRY_LIQ_MIN_TURNOVER_YEN", 10000000.0),
        _env_float("ENTRY_LIQ_MIN_RANGE_PCT", 0.0015),
        _env_float("ENTRY_LIQ_MIN_ATR_PCT", 0.0010),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY LIQ GUARD] auto install failed")

__all__ = ["install"]
