# -*- coding: utf-8 -*-
"""
Timebox volatility_filter ranking rescue.

SUMMARY_AI direct snapshot can hang inside volatility_filter when ranking rescue
scans multiple stale ranking DB files on NAS. This patch keeps the strict
fail-closed behavior: if a fresh ranking move cannot be read quickly, rescue is
not applied. It does not loosen ATR/range/board/order guards.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-VOL-FILTER-RANKING-RESCUE-TIMEBOX"
_INSTALLED = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _parse_dt(v: Any):
    try:
        if v is None:
            return None
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        import pandas as pd
        ts = pd.to_datetime(v, errors="coerce")
        if ts is None or getattr(ts, "isna", lambda: False)():
            return None
        x = ts.to_pydatetime()
        return x.replace(tzinfo=None) if isinstance(x, dt.datetime) else None
    except Exception:
        return None


def _coerce_ratio(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        if isinstance(v, str):
            s = v.replace("％", "%").replace("+", "").replace("%", "").strip()
            if not s:
                return 0.0
            x = float(s.replace(",", ""))
        else:
            x = float(v)
        return x / 100.0 if abs(x) > 1.0 else x
    except Exception:
        return 0.0


def _row_ratio(d: dict[str, Any]) -> float:
    for k in ("change_ratio", "change_rate", "change_percentage", "change_percent", "騰落率", "変化率"):
        if k in d:
            r = _coerce_ratio(d.get(k))
            if r:
                return r
    try:
        price = float(d.get("current_price") or d.get("price") or d.get("close") or d.get("close_price") or 0)
        prev = float(d.get("previous_close") or d.get("prev_close") or d.get("base_price") or d.get("reference_price") or 0)
        if price > 0 and prev > 0:
            return (price - prev) / prev
    except Exception:
        pass
    return 0.0


def _candidate_paths(vf: Any) -> list[str]:
    today = dt.datetime.now().strftime("%Y%m%d")
    out: list[str] = []
    for p in (os.getenv("RANKING_DB_PATH"), os.getenv("KABU_RANKING_DB_PATH"), os.getenv("ATS_RANKING_DB_PATH")):
        if p:
            out.append(str(p))
    # V1: Do not glob multiple old DBs. Old DB scans caused 10-100s stalls.
    for d in (
        os.getenv("RANKING_DB_DIR"),
        os.getenv("KABU_RANKING_DB_DIR"),
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\Ranking",
    ):
        if d:
            out.append(os.path.join(str(d), f"ranking{today}.db"))
    seen: set[str] = set()
    return [p for p in out if p and not (p in seen or seen.add(p))]


def _fast_latest_ranking_snapshot_move(symbol: str) -> dict[str, Any] | None:
    symbol = _norm_symbol(symbol)
    if not symbol:
        return None
    max_age = _env_float("RANKING_RESCUE_MAX_AGE_SEC", 360.0)
    deadline = time.perf_counter() + max(0.05, _env_float("VOL_FILTER_RANKING_RESCUE_TIMEBOX_SEC", 0.35))
    now_dt = dt.datetime.now()
    tables = ("ranking_snapshot_1min", "ranking", "値上がり率_ALL", "値下がり率_ALL", "売買高急増_ALL", "売買代金急増_ALL")
    for path in _candidate_paths(None):
        if time.perf_counter() > deadline:
            logger.warning("[VOL FILTER RESCUE TIMEBOX] deadline reached before path symbol=%s path=%s version=%s", symbol, path, VERSION)
            return None
        if not path or not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect(path, timeout=max(0.05, _env_float("VOL_FILTER_RANKING_RESCUE_SQLITE_TIMEOUT_SEC", 0.12)))
            conn.row_factory = sqlite3.Row
            try:
                existing = {r[0] for r in conn.execute("select name from sqlite_master where type='table'").fetchall()}
                for table in tables:
                    if time.perf_counter() > deadline:
                        logger.warning("[VOL FILTER RESCUE TIMEBOX] deadline reached symbol=%s path=%s table=%s version=%s", symbol, path, table, VERSION)
                        return None
                    if table not in existing:
                        continue
                    try:
                        cols = [r[1] for r in conn.execute(f'pragma table_info("{table}")').fetchall()]
                        if "symbol" not in cols:
                            continue
                        order_col = next((c for c in ("datetime", "snapshot_time", "received_at", "created_at", "inserted_at", "id") if c in cols), None)
                        sql = f'select * from "{table}" where cast(symbol as text)=?'
                        if order_col:
                            sql += f' order by "{order_col}" desc'
                        sql += " limit 1"
                        row = conn.execute(sql, (symbol,)).fetchone()
                        if row is None:
                            continue
                        d = dict(row)
                        dt_value = None
                        for c in ("datetime", "snapshot_time", "received_at", "created_at", "inserted_at"):
                            if c in d:
                                dt_value = _parse_dt(d.get(c))
                                if dt_value is not None:
                                    break
                        age_sec = abs((now_dt - dt_value).total_seconds()) if dt_value is not None else None
                        if age_sec is not None and age_sec > max_age:
                            logger.info("[VOL FILTER] ranking rescue stale-fast symbol=%s table=%s age=%.1fs max=%.1fs path=%s version=%s", symbol, table, age_sec, max_age, path, VERSION)
                            continue
                        ratio = _row_ratio(d)
                        return {"symbol": symbol, "ratio": ratio, "abs_ratio": abs(ratio), "price": float(d.get("current_price") or d.get("price") or d.get("close") or d.get("close_price") or 0), "table": table, "path": path, "datetime": dt_value.isoformat() if dt_value else None, "age_sec": age_sec}
                    except Exception:
                        logger.debug("[VOL FILTER RESCUE TIMEBOX] table scan failed symbol=%s path=%s table=%s", symbol, path, table, exc_info=True)
                        continue
            finally:
                conn.close()
        except Exception:
            logger.debug("[VOL FILTER RESCUE TIMEBOX] db scan failed symbol=%s path=%s", symbol, path, exc_info=True)
            continue
    return None


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("VOL_FILTER_RANKING_RESCUE_TIMEBOX_ENABLED", True):
        logger.warning("[VOL FILTER RESCUE TIMEBOX] disabled version=%s", VERSION)
        return False
    try:
        import trading.filters.volatility_filter as vf
        current = getattr(vf, "_latest_ranking_snapshot_move", None)
        if getattr(current, "_vol_filter_ranking_rescue_timebox_v1", False):
            _INSTALLED = True
            return True
        _fast_latest_ranking_snapshot_move._original = current  # type: ignore[attr-defined]
        _fast_latest_ranking_snapshot_move._vol_filter_ranking_rescue_timebox_v1 = True  # type: ignore[attr-defined]
        vf._latest_ranking_snapshot_move = _fast_latest_ranking_snapshot_move
        _INSTALLED = True
        logger.warning("[VOL FILTER RESCUE TIMEBOX] installed version=%s timebox=%.2fs sqlite_timeout=%.2fs old_db_glob=0", VERSION, _env_float("VOL_FILTER_RANKING_RESCUE_TIMEBOX_SEC", 0.35), _env_float("VOL_FILTER_RANKING_RESCUE_SQLITE_TIMEOUT_SEC", 0.12))
        return True
    except Exception:
        logger.exception("[VOL FILTER RESCUE TIMEBOX] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[VOL FILTER RESCUE TIMEBOX] auto install failed version=%s", VERSION)


__all__ = ["install", "VERSION"]
