# ============================================================
# File   : core/startup/entry_ma5_breakout_count_patch.py
# Version: V1.0-BUY-SELL-3M-5M-MA5-BREAKOUT-COUNT-GUARD
# ------------------------------------------------------------
# 【目的】
#   エントリー要素に、3分足/5分足で価格がMA5を抜けてから
#   何本目かを組み込む。
#
# 【BUY】
#   close が MA5 を下/同値から上へ抜けた足を1本目として数える。
#   3分足・5分足とも、指定本数以内ならBUY許可。
#
# 【SELL】
#   close が MA5 を上/同値から下へ抜けた足を1本目として数える。
#   3分足・5分足とも、指定本数以内ならSELL許可。
#
# デフォルト:
#   ENTRY_MA5_BREAKOUT_ENABLED=1
#   ENTRY_MA5_BREAKOUT_TFS=3,5
#   ENTRY_MA5_BREAKOUT_MIN_BAR=1
#   ENTRY_MA5_BREAKOUT_MAX_BAR=3
#   ENTRY_MA5_BREAKOUT_LOOKBACK=20
#   ENTRY_MA5_BREAKOUT_REQUIRE_DATA=1
# ============================================================

from __future__ import annotations

import logging
import math
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_DB_CACHE: dict[tuple[str, int, str], list[dict[str, Any]]] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.endswith(".T"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _ng(reason: str, **detail: Any) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


def _parse_tfs() -> list[int]:
    raw = os.getenv("ENTRY_MA5_BREAKOUT_TFS", "3,5")
    out: list[int] = []
    try:
        for x in str(raw).replace(";", ",").split(","):
            x = x.strip()
            if not x:
                continue
            v = int(float(x))
            if v in {1, 3, 5} and v not in out:
                out.append(v)
    except Exception:
        pass
    return out or [3, 5]


def _summary_db_path() -> str:
    ymd = os.getenv("KABU_TODAY") or os.getenv("TARGET_DATE") or datetime.now().strftime("%Y%m%d")
    root = os.getenv("SUMMARY_DB_DIR") or r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary"
    return os.path.join(root, f"summary{ymd}.db")


def _latest_rows_from_df(df: Any, symbol: str, limit: int) -> list[dict[str, Any]]:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return []
        sym = _norm_symbol(symbol)
        work = df.copy()
        work["__sym__"] = work["symbol"].map(_norm_symbol)
        work = work[work["__sym__"] == sym].copy()
        if work.empty:
            return []
        time_col = None
        for c in ("datetime", "end_time", "start_time", "time"):
            if c in work.columns:
                time_col = c
                break
        if time_col:
            work["__dt__"] = pd.to_datetime(work[time_col], errors="coerce")
            work = work.sort_values("__dt__", kind="stable")
        return [dict(x) for x in work.tail(limit).to_dict("records")]
    except Exception:
        logger.debug("[MA5 BREAKOUT] df rows lookup failed symbol=%s", symbol, exc_info=True)
        return []


def _history_from_gc(symbol: str, tf: int, limit: int) -> list[dict[str, Any]]:
    try:
        from core.global_context.context import global_context as GC
    except Exception:
        return []

    for getter_name in ("get_push_merged_summary", "get_merged_summary", "get_summary_history", "get_push_summary"):
        try:
            getter = getattr(GC, getter_name, None)
            if not callable(getter):
                continue
            if getter_name == "get_merged_summary":
                df = getter(tf, source="push")
            else:
                df = getter(tf)
            rows = _latest_rows_from_df(df, symbol, limit)
            if rows:
                return rows
        except Exception:
            logger.debug("[MA5 BREAKOUT] GC getter failed getter=%s tf=%s symbol=%s", getter_name, tf, symbol, exc_info=True)
    return []


def _history_from_db(symbol: str, tf: int, limit: int) -> list[dict[str, Any]]:
    if not _env_bool("ENTRY_MA5_BREAKOUT_DB_BACKFILL", True):
        return []

    sym = _norm_symbol(symbol)
    today = datetime.now().strftime("%Y%m%d")
    key = (sym, int(tf), today)
    if key in _DB_CACHE:
        return _DB_CACHE[key]

    db = _summary_db_path()
    table = f"stock_summary_{int(tf)}min"
    rows_out: list[dict[str, Any]] = []
    try:
        if not Path(db).exists():
            _DB_CACHE[key] = []
            return []

        sql = f"""
            SELECT datetime, close, close_price, price, ma5
              FROM {table}
             WHERE CAST(symbol AS TEXT)=?
             ORDER BY datetime DESC
             LIMIT ?
        """
        with sqlite3.connect(db, timeout=1.0) as conn:
            conn.execute("PRAGMA query_only=ON")
            got = conn.execute(sql, (sym, int(limit))).fetchall()

        for r in reversed(got or []):
            rows_out.append({
                "datetime": r[0],
                "close": r[1],
                "close_price": r[2],
                "price": r[3],
                "ma5": r[4],
            })
        if rows_out:
            logger.warning("[MA5 BREAKOUT] DB history symbol=%s tf=%s rows=%s db=%s", sym, tf, len(rows_out), db)
    except Exception as e:
        logger.debug("[MA5 BREAKOUT] DB history failed symbol=%s tf=%s err=%s", sym, tf, e, exc_info=False)

    _DB_CACHE[key] = rows_out
    return rows_out


def _history(symbol: str, tf: int, limit: int) -> list[dict[str, Any]]:
    rows = _history_from_gc(symbol, tf, limit)
    if len(rows) >= 2:
        return rows
    db_rows = _history_from_db(symbol, tf, limit)
    if len(db_rows) >= len(rows):
        return db_rows
    return rows


def _pick_price(row: dict[str, Any]) -> float:
    for k in ("close", "close_price", "price", "current_price"):
        v = _safe_float(row.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _pick_ma5(row: dict[str, Any]) -> float:
    for k in ("ma5", "MA5", "ma_5"):
        v = _safe_float(row.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _bars_since_ma5_break(rows: list[dict[str, Any]], side: str) -> tuple[Optional[int], dict[str, Any]]:
    """
    BUY : close > ma5 の連続本数。直前が above=False なら上抜け成立。
    SELL: close < ma5 の連続本数。直前が below=False なら下抜け成立。
    """
    if len(rows) < 2:
        return None, {"reason": "not_enough_rows", "rows": len(rows)}

    side_u = str(side or "").upper()
    states: list[dict[str, Any]] = []
    for r in rows:
        close = _pick_price(r)
        ma5 = _pick_ma5(r)
        if side_u == "SELL":
            active = bool(close > 0 and ma5 > 0 and close < ma5)
        else:
            active = bool(close > 0 and ma5 > 0 and close > ma5)
        states.append({
            "datetime": r.get("datetime") or r.get("end_time") or r.get("time"),
            "close": close,
            "ma5": ma5,
            "active": active,
            "side": side_u,
        })

    latest = states[-1]
    if not latest["active"]:
        return None, {"reason": "latest_not_broken_ma5", "latest": latest, "prev": states[-2] if len(states) >= 2 else None}

    run = 0
    for st in reversed(states):
        if bool(st["active"]):
            run += 1
        else:
            break

    break_index = len(states) - run
    prev = states[break_index - 1] if break_index - 1 >= 0 else None
    if prev is not None and bool(prev.get("active")):
        return None, {"reason": "break_not_found_in_lookback", "run_active": run, "latest": latest}

    return run, {"reason": "ok", "bars_since_break": run, "latest": latest, "prev_before_break": prev}


def _ma5_breakout_guard(entry_row: dict[str, Any], *, symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not _env_bool("ENTRY_MA5_BREAKOUT_ENABLED", True):
        return None
    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None
    if str(source or "").upper() != "SUMMARY_AI":
        return None

    sym = _norm_symbol(symbol or (entry_row or {}).get("symbol"))
    if not sym:
        return None

    min_bar = max(1, _env_int("ENTRY_MA5_BREAKOUT_MIN_BAR", 1))
    max_bar = max(min_bar, _env_int("ENTRY_MA5_BREAKOUT_MAX_BAR", 3))
    lookback = max(max_bar + 2, _env_int("ENTRY_MA5_BREAKOUT_LOOKBACK", 20))
    require_data = _env_bool("ENTRY_MA5_BREAKOUT_REQUIRE_DATA", True)

    details: dict[str, Any] = {}
    for tf in _parse_tfs():
        rows = _history(sym, tf, lookback)
        bars, diag = _bars_since_ma5_break(rows, side_u)
        details[f"{tf}m"] = {"bars": bars, **diag}

        if bars is None:
            if require_data:
                return _ng(
                    "MA5_BREAKOUT_MISSING_OR_NOT_BROKEN",
                    symbol=sym,
                    side=side_u,
                    tf=tf,
                    min_bar=min_bar,
                    max_bar=max_bar,
                    details=details,
                )
            continue
        if not (min_bar <= int(bars) <= max_bar):
            return _ng(
                "MA5_BREAKOUT_BAR_RANGE_NG",
                symbol=sym,
                side=side_u,
                tf=tf,
                bars_since_break=int(bars),
                min_bar=min_bar,
                max_bar=max_bar,
                details=details,
            )

    logger.warning(
        "[MA5 BREAKOUT] OK symbol=%s side=%s tfs=%s range=%s-%s details=%s",
        sym,
        side_u,
        _parse_tfs(),
        min_bar,
        max_bar,
        details,
    )
    return None


def _patch_guard_function(module: Any, attr: str, source_name: str) -> bool:
    old = getattr(module, attr, None)
    if not callable(old):
        return False
    if getattr(old, "_ma5_breakout_v1", False):
        return True

    def _wrapped(*args, **kwargs):
        ret = old(*args, **kwargs)
        if ret is not None:
            return ret
        try:
            symbol = kwargs.get("symbol") or ""
            side = kwargs.get("side") or ""
            row = kwargs.get("row") or kwargs.get("entry_row") or {}
            if args and isinstance(args[0], dict) and not row:
                row = args[0]
            if not isinstance(row, dict):
                row = {}
            ng = _ma5_breakout_guard(row, symbol=symbol, side=side, source="SUMMARY_AI")
            if ng is not None:
                logger.warning(
                    "[MA5 BREAKOUT] NG source=%s symbol=%s side=%s reason=%s detail=%s",
                    source_name,
                    symbol,
                    side,
                    ng.get("reason"),
                    ng.get("detail"),
                )
                return ng
        except Exception:
            logger.debug("[MA5 BREAKOUT] wrapped guard failed source=%s", source_name, exc_info=True)
        return None

    _wrapped._ma5_breakout_v1 = True  # type: ignore[attr-defined]
    _wrapped._original = old  # type: ignore[attr-defined]
    setattr(module, attr, _wrapped)
    return True


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    os.environ.setdefault("ENTRY_MA5_BREAKOUT_ENABLED", "1")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_TFS", "3,5")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_MIN_BAR", "1")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_MAX_BAR", "3")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_LOOKBACK", "20")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_REQUIRE_DATA", "1")
    os.environ.setdefault("ENTRY_MA5_BREAKOUT_DB_BACKFILL", "1")

    ok_any = False
    try:
        import trading.handlers.entry_order_builder as eob
        ok_any = _patch_guard_function(eob, "_summary_mtf_direction_guard", "entry_order_builder") or ok_any
    except Exception:
        logger.exception("[MA5 BREAKOUT] patch entry_order_builder failed")

    try:
        import core.startup.entry_limit_passive_runtime_patch as elp
        ok_any = _patch_guard_function(elp, "_summary_ai_strict_guard", "entry_limit_passive") or ok_any
    except Exception:
        logger.exception("[MA5 BREAKOUT] patch entry_limit_passive failed")

    _PATCHED = bool(ok_any)
    logger.warning(
        "[MA5 BREAKOUT] installed=%s enabled=%s tfs=%s bar_range=%s-%s lookback=%s require_data=%s db_backfill=%s",
        _PATCHED,
        os.getenv("ENTRY_MA5_BREAKOUT_ENABLED"),
        os.getenv("ENTRY_MA5_BREAKOUT_TFS"),
        os.getenv("ENTRY_MA5_BREAKOUT_MIN_BAR"),
        os.getenv("ENTRY_MA5_BREAKOUT_MAX_BAR"),
        os.getenv("ENTRY_MA5_BREAKOUT_LOOKBACK"),
        os.getenv("ENTRY_MA5_BREAKOUT_REQUIRE_DATA"),
        os.getenv("ENTRY_MA5_BREAKOUT_DB_BACKFILL"),
    )
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[MA5 BREAKOUT] auto install failed")

__all__ = ["install"]
