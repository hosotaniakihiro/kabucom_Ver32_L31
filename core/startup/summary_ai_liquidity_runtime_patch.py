# ============================================================
# File   : core/startup/summary_ai_liquidity_runtime_patch.py
# Version: V2.1-RECENT-SUMMARY-TURNOVER-FALLBACK-FIX
# ------------------------------------------------------------
# 目的:
#   SUMMARY_AI の approved_rows 作成前に、出来高/売買代金の足切りを入れる。
#
# Ver2.1:
#   - summary DB の turnover が小さい単位/不完全値の場合、
#     AI row 側の row_turnover や close*volume を fallback として採用する。
#   - ログ上で row_turnover は十分なのに turnover が小さく、
#     SUMMARY_AI_LIQ_TURNOVER_LOW で落ちる問題を修正。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)
_INSTALLED = False


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


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path() -> str:
    base = os.getenv("SUMMARY_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary")
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_today()}.db"))


def _pick(d: Dict[str, Any], names: list[str]) -> Any:
    for n in names:
        if n in d and d.get(n) not in (None, ""):
            return d.get(n)
    return None


def _merged_item(item: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in ("source_row", "ai_row"):
        v = item.get(k)
        if isinstance(v, dict):
            out.update(v)
    out.update(item)
    return out


def _col(conn: sqlite3.Connection, table: str, names: list[str]) -> str:
    try:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for n in names:
            if n in cols:
                return n
    except Exception:
        return ""
    return ""


def _recent_summary_values(symbol: str) -> Dict[str, Any]:
    if not _env_bool("SUMMARY_AI_LIQ_USE_RECENT_SUMMARY", True):
        return {}

    path = _summary_db_path()
    table = os.getenv("SUMMARY_AI_LIQ_SUMMARY_TABLE", "stock_summary_1min")
    bars = max(1, _env_int("SUMMARY_AI_LIQ_RECENT_BARS", 5))

    if not symbol or not Path(path).exists():
        return {}

    try:
        with sqlite3.connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            sym = _col(conn, table, ["symbol", "code", "stock_code"])
            tm = _col(conn, table, ["datetime", "dt", "timestamp", "time"])
            cl = _col(conn, table, ["close_price", "close", "price", "current_price"])
            vo = _col(conn, table, ["volume", "Volume", "vol", "出来高"])
            tv = _col(conn, table, ["turnover", "turnover_yen", "trading_value", "売買代金"])
            if not sym or not tm or not cl:
                return {}
            select = f"{tm}, {cl}, {vo or '0'}, {tv or '0'}"
            rows = conn.execute(
                f"SELECT {select} FROM {table} WHERE CAST({sym} AS TEXT)=? ORDER BY {tm} DESC LIMIT ?",
                (_sym(symbol), bars),
            ).fetchall()

        if not rows:
            return {}

        close = _f(rows[0][1], 0.0)
        volume = sum(max(0.0, _f(r[2], 0.0)) for r in rows)
        turnover = sum(max(0.0, _f(r[3], 0.0)) for r in rows)
        if turnover <= 0 and close > 0 and volume > 0:
            turnover = close * volume

        return {
            "liq_mode": "recent_summary_1min",
            "liq_bars": len(rows),
            "liq_latest_dt": str(rows[0][0]),
            "close": close,
            "volume": volume,
            "turnover": turnover,
            "summary_db": path,
        }
    except Exception:
        logger.debug("[SUMMARY AI LIQ GUARD] recent summary read failed symbol=%s path=%s", symbol, path, exc_info=True)
        return {}


def _row_values(item: Dict[str, Any]) -> Dict[str, Any]:
    row = _merged_item(item)
    symbol = _sym(_pick(row, ["symbol", "code", "stock_code"]))
    close = _f(_pick(row, ["close_price", "close", "price", "current_price"]), 0.0)
    volume = _f(_pick(row, ["volume", "Volume", "vol", "出来高"]), 0.0)
    turnover = _f(_pick(row, ["turnover", "turnover_yen", "trading_value", "売買代金"]), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    return {
        "symbol": symbol,
        "liq_mode": "ai_row_fallback",
        "liq_bars": 1,
        "close": close,
        "volume": volume,
        "turnover": turnover,
    }


def _liquidity_values(item: Dict[str, Any]) -> Dict[str, Any]:
    row_v = _row_values(item)
    symbol = row_v.get("symbol") or ""
    recent = _recent_summary_values(symbol)
    if recent:
        recent["symbol"] = symbol
        row_volume = _f(row_v.get("volume"), 0.0)
        row_turnover = _f(row_v.get("turnover"), 0.0)
        recent_turnover = _f(recent.get("turnover"), 0.0)
        recent_volume = _f(recent.get("volume"), 0.0)
        close = _f(recent.get("close"), _f(row_v.get("close"), 0.0))

        # DB側turnoverが小さい単位/不完全値の場合は、row_turnoverを優先する。
        # row_turnoverが無ければ close * max(volume) を使う。
        calc_turnover = 0.0
        if close > 0:
            calc_turnover = close * max(recent_volume, row_volume)

        fixed_turnover = max(recent_turnover, row_turnover, calc_turnover)
        if fixed_turnover > recent_turnover:
            recent["turnover_original"] = recent_turnover
            recent["turnover_fixed_by"] = "max_recent_row_calc"
            recent["turnover"] = fixed_turnover

        if row_volume > recent_volume:
            recent["volume_original"] = recent_volume
            recent["volume_fixed_by"] = "row_volume"
            recent["volume"] = row_volume

        recent["row_volume"] = row_volume
        recent["row_turnover"] = row_turnover
        return recent
    return row_v


def _liquidity_ok(item: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
    v = _liquidity_values(item)
    symbol = str(v.get("symbol") or "")
    close = _f(v.get("close"), 0.0)
    volume = _f(v.get("volume"), 0.0)
    turnover = _f(v.get("turnover"), 0.0)

    min_price = _env_float("SUMMARY_AI_LIQ_MIN_PRICE", 200.0)
    min_volume = _env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 10000000.0)

    detail = {
        "symbol": symbol,
        **v,
        "min_price": min_price,
        "min_volume": min_volume,
        "min_turnover": min_turnover,
        "require_data": _env_bool("SUMMARY_AI_LIQ_REQUIRE_DATA", True),
    }

    if _env_bool("SUMMARY_AI_LIQ_REQUIRE_DATA", True):
        if close <= 0:
            return False, "SUMMARY_AI_LIQ_NO_CLOSE", detail
        if volume <= 0:
            return False, "SUMMARY_AI_LIQ_NO_VOLUME", detail

    if close < min_price:
        return False, "SUMMARY_AI_LIQ_PRICE_LOW", detail
    if volume < min_volume:
        return False, "SUMMARY_AI_LIQ_VOLUME_LOW", detail
    if turnover < min_turnover:
        return False, "SUMMARY_AI_LIQ_TURNOVER_LOW", detail
    return True, "OK", detail


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry.summary_ai.executor as ex
    except Exception:
        logger.exception("[SUMMARY AI LIQ GUARD] import executor failed")
        return False

    old = getattr(ex, "_filter_blocked_ai_ok_items", None)
    if not callable(old):
        logger.warning("[SUMMARY AI LIQ GUARD] _filter_blocked_ai_ok_items missing")
        return False

    if not getattr(old, "_summary_ai_liq_wrapped_v21", False):
        def wrapped(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            base = old(ok_items)
            kept: List[Dict[str, Any]] = []
            skipped: List[Dict[str, Any]] = []
            for item in base:
                if not isinstance(item, dict):
                    continue
                ok, reason, detail = _liquidity_ok(item)
                if ok:
                    kept.append(item)
                else:
                    skipped.append({"reason": reason, **detail})
            if skipped:
                logger.warning(
                    "[SUMMARY AI LIQ GUARD] filtered before approved rows before=%s after=%s skipped=%s",
                    len(base), len(kept), skipped[:80],
                )
            else:
                logger.info(
                    "[SUMMARY AI LIQ GUARD] passed before approved rows count=%s min_volume=%s min_turnover=%s",
                    len(base),
                    _env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0),
                    _env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 10000000.0),
                )
            return kept

        wrapped._summary_ai_liq_wrapped_v21 = True  # type: ignore[attr-defined]
        wrapped._original = old  # type: ignore[attr-defined]
        ex._filter_blocked_ai_ok_items = wrapped

    _INSTALLED = True
    logger.warning(
        "[SUMMARY AI LIQ GUARD] installed v2.1 min_volume=%s min_turnover=%s min_price=%s recent_bars=%s require_data=%s turnover_fallback=row_or_calc",
        _env_float("SUMMARY_AI_LIQ_MIN_VOLUME", 30000.0),
        _env_float("SUMMARY_AI_LIQ_MIN_TURNOVER_YEN", 10000000.0),
        _env_float("SUMMARY_AI_LIQ_MIN_PRICE", 200.0),
        _env_int("SUMMARY_AI_LIQ_RECENT_BARS", 5),
        _env_bool("SUMMARY_AI_LIQ_REQUIRE_DATA", True),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI LIQ GUARD] auto install failed")

__all__ = ["install"]