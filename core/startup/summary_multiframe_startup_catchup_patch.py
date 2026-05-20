# ============================================================
# File   : core/startup/summary_multiframe_startup_catchup_patch.py
# Version: V1.2-RUN-INDICATOR-FILL-AFTER-CATCHUP
# ------------------------------------------------------------
# 起動時にDB内の1分足サマリーから、3分足・5分足サマリーを差分再集計する。
# その後、3分足/5分足の rsi/macd/signal/ma75/score 等を補完する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_INSTALLED = False
_RUNNING = False
_LOCK = threading.Lock()


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _summary_db_path() -> str:
    explicit = os.getenv("SUMMARY_DB_PATH") or os.getenv("AUTOSTOCK_SUMMARY_DB_PATH")
    if explicit:
        return explicit
    base = os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )
    today = dt.datetime.now().strftime("%Y%m%d")
    return str(Path(base) / f"summary{today}.db")


def _parse_intervals() -> list[int]:
    raw = os.getenv("SUMMARY_MTF_CATCHUP_INTERVALS", "3,5")
    out: list[int] = []
    for x in str(raw).replace(";", ",").split(","):
        try:
            n = int(float(x.strip()))
            if n in (3, 5) and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [3, 5]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
    except Exception:
        return False


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _pick(cols: Iterable[str], candidates: Iterable[str]) -> str:
    s = set(cols or [])
    for c in candidates:
        if c in s:
            return c
    return ""


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if v is None:
            return None
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        txt = str(v).strip().replace("T", " ")
        if not txt:
            return None
        return dt.datetime.fromisoformat(txt)
    except Exception:
        try:
            import pandas as pd
            t = pd.to_datetime(v, errors="coerce")
            if pd.isna(t):
                return None
            if hasattr(t, "to_pydatetime"):
                t = t.to_pydatetime()
            if isinstance(t, dt.datetime):
                return t.replace(tzinfo=None)
        except Exception:
            pass
    return None


def _floor_interval(t: dt.datetime, interval: int) -> dt.datetime:
    return t.replace(minute=(t.minute // interval) * interval, second=0, microsecond=0)


def _latest_dst_dt(conn: sqlite3.Connection, table: str, cols: list[str]) -> dt.datetime | None:
    dt_col = _pick(cols, ["datetime", "dt", "timestamp"])
    date_col = _pick(cols, ["date", "trade_date"])
    time_col = _pick(cols, ["time", "minute", "bar_time"])
    try:
        if dt_col:
            r = conn.execute(f"SELECT MAX({dt_col}) FROM {table}").fetchone()
            return _parse_dt(r[0] if r else None)
        if date_col and time_col:
            r = conn.execute(f"SELECT MAX({date_col} || ' ' || {time_col}) FROM {table}").fetchone()
            return _parse_dt(r[0] if r else None)
    except Exception:
        logger.debug("[SUMMARY MTF CATCHUP] latest dt failed table=%s", table, exc_info=True)
    return None


def _build_1min_select(cols: list[str], table: str) -> str:
    sym = _pick(cols, ["symbol", "code", "stock_code"])
    dtc = _pick(cols, ["datetime", "dt", "timestamp"])
    datec = _pick(cols, ["date", "trade_date"])
    timec = _pick(cols, ["time", "minute", "bar_time"])
    name = _pick(cols, ["symbolname", "symbol_name", "name"])
    op = _pick(cols, ["open_price", "open"])
    hi = _pick(cols, ["high_price", "high"])
    lo = _pick(cols, ["low_price", "low"])
    cl = _pick(cols, ["close_price", "close", "price", "current_price"])
    vol = _pick(cols, ["volume", "vol"])
    turn = _pick(cols, ["turnover", "turnover_yen", "trading_value"])
    vwap = _pick(cols, ["vwap"])

    if not sym:
        raise RuntimeError("stock_summary_1min symbol column missing")
    if not dtc and not (datec and timec):
        raise RuntimeError("stock_summary_1min datetime columns missing")
    if not cl:
        raise RuntimeError("stock_summary_1min close/price column missing")

    dtexpr = dtc if dtc else f"({datec} || ' ' || {timec})"
    return f"""
        SELECT
            CAST({sym} AS TEXT) AS symbol,
            {name if name else "''"} AS symbolname,
            {dtexpr} AS dtv,
            {op if op else cl} AS open_price,
            {hi if hi else cl} AS high_price,
            {lo if lo else cl} AS low_price,
            {cl} AS close_price,
            {vol if vol else "0"} AS volume,
            {turn if turn else "0"} AS turnover,
            {vwap if vwap else "0"} AS vwap
        FROM {table}
        WHERE {dtexpr} >= ?
        ORDER BY symbol, dtv
        LIMIT ?
    """


def _aggregate(rows: list[tuple[Any, ...]], interval: int, include_current_partial: bool) -> list[dict[str, Any]]:
    now_floor = _floor_interval(dt.datetime.now(), interval)
    buckets: dict[tuple[str, dt.datetime], list[dict[str, Any]]] = {}

    for r in rows:
        sym = _norm_symbol(r[0])
        dtx = _parse_dt(r[2])
        if not sym or dtx is None:
            continue
        bdt = _floor_interval(dtx, interval)
        if not include_current_partial and bdt >= now_floor:
            continue
        buckets.setdefault((sym, bdt), []).append({
            "symbol": sym,
            "symbolname": str(r[1] or ""),
            "datetime": dtx,
            "open_price": _f(r[3], 0.0),
            "high_price": _f(r[4], 0.0),
            "low_price": _f(r[5], 0.0),
            "close_price": _f(r[6], 0.0),
            "volume": _f(r[7], 0.0),
            "turnover": _f(r[8], 0.0),
            "vwap": _f(r[9], 0.0),
        })

    out: list[dict[str, Any]] = []
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for (sym, bdt), xs in sorted(buckets.items(), key=lambda x: (x[0][1], x[0][0])):
        xs = sorted(xs, key=lambda x: x["datetime"])
        closes = [_f(x.get("close_price"), 0.0) for x in xs if _f(x.get("close_price"), 0.0) > 0]
        if not closes:
            continue
        open_price = next((_f(x.get("open_price"), 0.0) for x in xs if _f(x.get("open_price"), 0.0) > 0), closes[0])
        close_price = closes[-1]
        highs = [_f(x.get("high_price"), 0.0) for x in xs if _f(x.get("high_price"), 0.0) > 0]
        lows = [_f(x.get("low_price"), 0.0) for x in xs if _f(x.get("low_price"), 0.0) > 0]
        volume = sum(_f(x.get("volume"), 0.0) for x in xs)
        turnover = sum(_f(x.get("turnover"), 0.0) for x in xs)
        if turnover <= 0 and close_price > 0 and volume > 0:
            turnover = close_price * volume
        vwap = turnover / volume if turnover > 0 and volume > 0 else 0.0
        out.append({
            "symbol": sym,
            "symbolname": xs[-1].get("symbolname") or xs[0].get("symbolname") or "",
            "symbol_name": xs[-1].get("symbolname") or xs[0].get("symbolname") or "",
            "datetime": bdt.strftime("%Y-%m-%d %H:%M:%S"),
            "date": bdt.strftime("%Y-%m-%d"),
            "time": bdt.strftime("%H:%M:%S"),
            "open_price": open_price,
            "high_price": max(highs) if highs else close_price,
            "low_price": min(lows) if lows else close_price,
            "close_price": close_price,
            "open": open_price,
            "high": max(highs) if highs else close_price,
            "low": min(lows) if lows else close_price,
            "close": close_price,
            "price": close_price,
            "current_price": close_price,
            "volume": volume,
            "turnover": turnover,
            "trading_value": turnover,
            "vwap": vwap,
            "source": f"startup_1min_to_{interval}min_catchup",
            "updated_at": now_str,
        })
    return out


def _upsert(conn: sqlite3.Connection, table: str, cols: list[str], rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    preferred = [
        "symbol", "symbolname", "symbol_name", "datetime", "date", "time",
        "open_price", "high_price", "low_price", "close_price",
        "open", "high", "low", "close", "price", "current_price",
        "volume", "turnover", "trading_value", "vwap", "source", "updated_at",
    ]
    writable = [c for c in preferred if c in cols]
    if "symbol" not in writable:
        raise RuntimeError(f"{table} symbol column missing")
    if "datetime" in writable:
        conflict = "symbol, datetime"
    elif "date" in writable and "time" in writable:
        conflict = "symbol, date, time"
    else:
        raise RuntimeError(f"{table} datetime/date+time columns missing")
    non_keys = [c for c in writable if c not in {"symbol", "datetime", "date", "time"}]
    update_sql = ",".join([f"{c}=excluded.{c}" for c in non_keys])
    sql = f"""
        INSERT INTO {table} ({','.join(writable)})
        VALUES ({','.join(['?'] * len(writable))})
        ON CONFLICT({conflict}) DO UPDATE SET {update_sql}
    """
    vals = [tuple(r.get(c) for c in writable) for r in rows]
    conn.executemany(sql, vals)
    return len(vals)


def _read_recent_from_db(conn: sqlite3.Connection, table: str, cols: list[str], interval: int, bars: int) -> list[dict[str, Any]]:
    sym = _pick(cols, ["symbol", "code", "stock_code"])
    dtc = _pick(cols, ["datetime", "dt", "timestamp"])
    datec = _pick(cols, ["date", "trade_date"])
    timec = _pick(cols, ["time", "minute", "bar_time"])
    cl = _pick(cols, ["close_price", "close", "price", "current_price"])
    vol = _pick(cols, ["volume", "vol"])
    if not sym or not cl or not (dtc or (datec and timec)):
        return []
    dtexpr = dtc if dtc else f"({datec} || ' ' || {timec})"
    lookback_min = max(interval * bars + 30, interval * bars)
    since = (dt.datetime.now() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        f"SELECT CAST({sym} AS TEXT), {dtexpr}, {cl}, {vol if vol else '0'} FROM {table} WHERE {dtexpr} >= ? ORDER BY {dtexpr} DESC LIMIT ?",
        (since, max(1000, bars * 200)),
    ).fetchall()
    return [{"symbol": _norm_symbol(r[0]), "datetime": str(r[1]), "close": _f(r[2], 0.0), "volume": _f(r[3], 0.0)} for r in rows]


def _run_indicator_fill_after_catchup() -> None:
    if not _env_bool("SUMMARY_MTF_INDICATOR_FILL_AFTER_CATCHUP", True):
        return
    try:
        from core.startup.summary_mtf_indicator_fill_patch import run_fill
        run_fill(reason="after_mtf_catchup")
    except Exception as e:
        logger.warning("[SUMMARY MTF CATCHUP] indicator fill failed err=%s", e, exc_info=True)


def run_catchup(*, reason: str = "manual") -> dict[str, Any]:
    t0 = time.monotonic()
    path = _summary_db_path()
    src = os.getenv("SUMMARY_MTF_CATCHUP_SRC_TABLE", "stock_summary_1min")
    intervals = _parse_intervals()
    ma_bars = _env_int("SUMMARY_MTF_CATCHUP_MA_BARS", 75)
    extra_min = _env_int("SUMMARY_MTF_CATCHUP_EXTRA_MINUTES", 30)
    max_rows = _env_int("SUMMARY_MTF_CATCHUP_MAX_ROWS", 80000)
    include_partial = _env_bool("SUMMARY_MTF_CATCHUP_INCLUDE_CURRENT_PARTIAL", True)
    result: dict[str, Any] = {"ok": False, "path": path, "intervals": intervals, "reason": reason, "details": {}}

    if not Path(path).exists():
        result["error"] = "summary_db_not_found"
        logger.warning("[SUMMARY MTF CATCHUP] skip db not found path=%s", path)
        return result

    try:
        with sqlite3.connect(path, timeout=_env_float("SUMMARY_MTF_CATCHUP_SQLITE_TIMEOUT", 5.0)) as conn:
            conn.execute("PRAGMA busy_timeout=%d" % int(_env_float("SUMMARY_MTF_CATCHUP_BUSY_TIMEOUT_MS", 5000)))
            if not _table_exists(conn, src):
                result["error"] = "src_table_missing"
                logger.warning("[SUMMARY MTF CATCHUP] skip src missing table=%s path=%s", src, path)
                return result
            cols1 = _columns(conn, src)
            sql1 = _build_1min_select(cols1, src)
            total_upserted = 0
            for interval in intervals:
                dst = f"stock_summary_{interval}min"
                detail: dict[str, Any] = {"dst": dst, "interval": interval}
                result["details"][interval] = detail
                if not _table_exists(conn, dst):
                    detail["error"] = "dst_table_missing"
                    logger.warning("[SUMMARY MTF CATCHUP] skip interval=%s dst table missing path=%s", interval, path)
                    continue
                cols_dst = _columns(conn, dst)
                latest = _latest_dst_dt(conn, dst, cols_dst)
                lookback_min = max(interval * ma_bars + extra_min, interval * ma_bars)
                floor_since = dt.datetime.now() - dt.timedelta(minutes=lookback_min)
                if latest is None:
                    since = floor_since
                    latest_s = None
                else:
                    since = min(latest - dt.timedelta(minutes=1), floor_since)
                    latest_s = latest.strftime("%Y-%m-%d %H:%M:%S")
                rows1 = conn.execute(sql1, (since.strftime("%Y-%m-%d %H:%M:%S"), max_rows)).fetchall()
                barsx = _aggregate(rows1, interval, include_partial)
                upserted = _upsert(conn, dst, cols_dst, barsx)
                recent = _read_recent_from_db(conn, dst, cols_dst, interval, ma_bars)
                total_upserted += upserted
                detail.update({
                    "latest_before": latest_s,
                    "since": since.strftime("%Y-%m-%d %H:%M:%S"),
                    "rows_1min": len(rows1),
                    "bars": len(barsx),
                    "upserted": upserted,
                    "recent_rows_for_ma": len(recent),
                    "ma_bars": ma_bars,
                })
                logger.warning(
                    "[SUMMARY MTF CATCHUP] interval=%s latest_before=%s since=%s rows_1min=%s bars=%s upserted=%s recent_for_ma=%s",
                    interval, latest_s, detail["since"], len(rows1), len(barsx), upserted, len(recent),
                )
            conn.commit()
        result.update({"ok": True, "upserted": total_upserted, "elapsed": round(time.monotonic() - t0, 3)})
        logger.warning("[SUMMARY MTF CATCHUP] done reason=%s upserted=%s elapsed=%.3fs db=%s", reason, total_upserted, time.monotonic() - t0, path)
        _run_indicator_fill_after_catchup()
        return result
    except Exception as e:
        result["error"] = str(e)
        logger.warning("[SUMMARY MTF CATCHUP] failed reason=%s err=%s path=%s", reason, e, path, exc_info=True)
        return result


def _run_background(reason: str) -> None:
    global _RUNNING
    with _LOCK:
        if _RUNNING:
            logger.info("[SUMMARY MTF CATCHUP] already running skip reason=%s", reason)
            return
        _RUNNING = True
    try:
        run_catchup(reason=reason)
    finally:
        with _LOCK:
            _RUNNING = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_MTF_STARTUP_CATCHUP_ENABLED", True):
        logger.warning("[SUMMARY MTF CATCHUP] disabled by env")
        return False
    _INSTALLED = True
    logger.warning(
        "[SUMMARY MTF CATCHUP] installed intervals=%s ma_bars=%s async=%s",
        _parse_intervals(),
        _env_int("SUMMARY_MTF_CATCHUP_MA_BARS", 75),
        _env_bool("SUMMARY_MTF_CATCHUP_RUN_ASYNC", True),
    )
    if _env_bool("SUMMARY_MTF_CATCHUP_RUN_ASYNC", True):
        th = threading.Thread(target=_run_background, args=("startup",), name="summary-mtf-startup-catchup", daemon=True)
        th.start()
    else:
        _run_background("startup")
    return True

try:
    install()
except Exception as e:
    logger.warning("[SUMMARY MTF CATCHUP] auto install failed err=%s", e, exc_info=False)

__all__ = ["install", "run_catchup"]