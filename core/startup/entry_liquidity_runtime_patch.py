# ============================================================
# File   : core/startup/entry_liquidity_runtime_patch.py
# Version: V1.4-ENTRY-LIQUIDITY-TURNOVER-YEN-NORMALIZE-FIX
# ------------------------------------------------------------
# 目的:
#   出来高が少ない・売買代金が薄い・値動きが小さい銘柄への
#   新規エントリーを止める。
#
# V1.4:
#   - recent summary DB 側の turnover が「百万円単位/千円単位/不完全値」でも、
#     close * volume から円単位 turnover_yen を再計算して比較する。
#   - ログ例: close=1813 volume=1440900 turnover=2596.44 min_turnover=10000000
#     のように、本来は約26億円ある銘柄が LIQUIDITY_RECENT_TURNOVER_LOW で
#     誤って落ちる問題を修正。
#   - detail に turnover_original / turnover_fixed_by / turnover_calc_yen を出す。
#
# V1.3:
#   - ec._log_skip(symbol, reason, **detail) 呼び出し時、
#     detail 内にも symbol/side が入っていると、別patch済み _log_skip が
#     TypeError: got multiple values for argument 'symbol' を出すため除外する。
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


def _calc_turnover_yen(close: float, volume: float) -> float:
    """株価×株数から円単位の売買代金を計算する。"""
    close = _f(close, 0.0)
    volume = _f(volume, 0.0)
    if close <= 0 or volume <= 0:
        return 0.0
    return close * volume


def _normalize_turnover_yen(turnover: float, close: float, volume: float) -> Tuple[float, Dict[str, Any]]:
    """
    turnover を必ず円単位として扱える値に補正する。

    summary DB の turnover は環境によって、
      - 円単位
      - 千円単位
      - 百万円単位
      - 途中計算の小さい値
    が混在しうる。

    エントリー流動性ガードの閾値 ENTRY_LIQ_MIN_TURNOVER_YEN は円単位なので、
    close * volume が取れる場合はそれを安全側の円単位候補として使う。
    """
    raw = max(0.0, _f(turnover, 0.0))
    calc = _calc_turnover_yen(close, volume)
    fixed = raw
    info: Dict[str, Any] = {"turnover_calc_yen": calc}

    if calc > 0:
        # raw が 0、または close*volume より極端に小さい場合は単位違い/不完全値とみなす。
        # 例: raw=2596.44, calc=2,612,351,700 のようなケース。
        if raw <= 0 or raw < calc * 0.5:
            fixed = calc
            info["turnover_original"] = raw
            info["turnover_fixed_by"] = "close_x_volume_yen"
        else:
            fixed = raw

    return fixed, info


def _entry_row_values(row: Dict[str, Any]) -> Dict[str, Any]:
    close = _first(row, ["close_price", "close", "price", "current_price"])
    volume = _first(row, ["volume", "Volume", "vol", "出来高"])
    high = _first(row, ["high_price", "high"])
    low = _first(row, ["low_price", "low"])
    atr = _first(row, ["atr", "atr_1m", "atr_3m", "atr_5m"])
    turnover_raw = _first(row, ["turnover_yen", "turnover", "trading_value", "売買代金"])
    turnover, turnover_info = _normalize_turnover_yen(turnover_raw, close, volume)

    return {
        "liq_mode": "entry_row_fallback",
        "liq_bars": 1,
        "close": close,
        "volume": volume,
        "turnover": turnover,
        **turnover_info,
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
            tv = _col(conn, table, ["turnover_yen", "turnover", "trading_value", "売買代金"])
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
        turnover_raw = sum(max(0.0, _f(r[5], 0.0)) for r in rows)
        turnover_calc_yen = sum(
            _calc_turnover_yen(_f(r[1], 0.0), _f(r[4], 0.0))
            for r in rows
        )
        turnover, turnover_info = _normalize_turnover_yen(turnover_raw, close, volume)

        # 複数barでは「最新close×合計volume」より「各bar close×volume 合計」の方が正確。
        if turnover_calc_yen > 0 and turnover < turnover_calc_yen * 0.5:
            turnover_info["turnover_original"] = turnover
            turnover_info["turnover_fixed_by"] = "sum_close_x_volume_yen"
            turnover_info["turnover_calc_yen"] = turnover_calc_yen
            turnover = turnover_calc_yen
        elif turnover_calc_yen > 0:
            turnover_info["turnover_calc_yen"] = turnover_calc_yen

        atrs = [_f(r[6], 0.0) for r in rows if _f(r[6], 0.0) > 0]
        return {
            "liq_mode": "recent_summary_1min",
            "liq_bars": len(rows),
            "liq_latest_dt": str(rows[0][0]),
            "close": close,
            "volume": volume,
            "turnover": turnover,
            **turnover_info,
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


def _safe_log_skip(ec: Any, symbol: str, reason: str, detail: Dict[str, Any]) -> None:
    """patched _log_skip の引数衝突を避ける。"""
    try:
        safe_detail = dict(detail or {})
        # _log_skip(symbol, reason, **kwargs) の positional と衝突するキーを除外
        safe_detail.pop("symbol", None)
        # patched_log_skip 側で side も positional/keyword化されている可能性があるため除外
        safe_detail.pop("side", None)
        ec._log_skip(str(symbol), reason, **safe_detail)
    except TypeError:
        try:
            ec._log_skip(str(symbol), reason)
        except Exception:
            logger.warning("[ENTRY LIQ GUARD] _log_skip failed symbol=%s reason=%s detail=%s", symbol, reason, detail)
    except Exception:
        logger.warning("[ENTRY LIQ GUARD] _log_skip failed symbol=%s reason=%s detail=%s", symbol, reason, detail)


def _install_summary_ai_liquidity_guard() -> None:
    try:
        from core.startup.summary_ai_liquidity_runtime_patch import install as install_summary_ai_liq
        ok = install_summary_ai_liq()
        logger.warning("[ENTRY LIQ GUARD] summary_ai_liquidity_runtime_patch installed=%s", ok)
    except Exception:
        logger.exception("[ENTRY LIQ GUARD] summary_ai_liquidity_runtime_patch install failed")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        _install_summary_ai_liquidity_guard()
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
    if not getattr(old, "_entry_liq_guard_wrapped_v14", False):
        def wrapped(item: dict, boost_active: bool) -> bool:
            try:
                row = item.get("entry_row") if isinstance(item, dict) else None
                if isinstance(row, dict):
                    row.setdefault("symbol", item.get("symbol"))
                    row.setdefault("side", item.get("side"))
                    ok, reason, detail = _check_liquidity(row)
                    if not ok:
                        _safe_log_skip(ec, str(item.get("symbol")), reason, detail)
                        return False
            except Exception:
                logger.exception("[ENTRY LIQ GUARD] precheck failed")
                return False
            return old(item, boost_active=boost_active)
        wrapped._entry_liq_guard_wrapped_v14 = True  # type: ignore[attr-defined]
        wrapped._original = old  # type: ignore[attr-defined]
        ec._execute_best_candidate = wrapped
    _install_summary_ai_liquidity_guard()
    _INSTALLED = True
    logger.warning(
        "[ENTRY LIQ GUARD] installed v1.4 entry_controller+summary_ai min_volume=%s min_turnover_yen=%s recent_bars=%s min_range_pct=%s min_atr_pct=%s turnover_normalize=close_x_volume_yen",
        _env_float("ENTRY_LIQ_MIN_VOLUME", 30000.0),
        _env_float("ENTRY_LIQ_MIN_TURNOVER_YEN", 10000000.0),
        _env_int("ENTRY_LIQ_RECENT_BARS", 5),
        _env_float("ENTRY_LIQ_MIN_RANGE_PCT", 0.0015),
        _env_float("ENTRY_LIQ_MIN_ATR_PCT", 0.0010),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY LIQ GUARD] auto install failed")

__all__ = ["install"]
