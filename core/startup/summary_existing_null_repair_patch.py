# ============================================================
# File   : core/startup/summary_existing_null_repair_patch.py
# Version: V1.0-REPAIR-EXISTING-SUMMARY-NULLS
# ------------------------------------------------------------
# 目的:
#   既に summaryYYYYMMDD.db に入っている stock_summary_1min/3min/5min の
#   NULL項目を起動時に再計算・補修する。
#
# 背景:
#   summary_multiframe_startup_catchup_patch / summary_mtf_indicator_fill_patch は
#   新規UPSERTや直近補完には効くが、既存DB内の古いNULL行が残ることがある。
#   このパッチは既存行を読み直し、NULLが残っている行だけUPDATEする。
#
# 補修対象:
#   time_range
#   rsi / macd / signal / atr
#   slope / slope_atr_scaled / score_slope
#   ma5 / ma25 / ma75
#   score / score_buy / score_sell / score_total
#   final_score / display_score
#   score_mtf / mtf_score / mtf
#
# 安全性:
#   - DBに存在するカラムだけ更新
#   - NULLが残っている行だけ更新
#   - chunk commit + busy retry
#   - 失敗しても本体起動を止めない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import random
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
    raw = os.getenv("SUMMARY_EXISTING_NULL_REPAIR_INTERVALS", "1,3,5")
    out: list[int] = []
    for x in str(raw).replace(";", ",").split(","):
        try:
            n = int(float(x.strip()))
            if n in (1, 3, 5) and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [1, 3, 5]


def _is_locked_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return "database is locked" in msg or "database table is locked" in msg or "database busy" in msg or "locked" in msg


def _rollback_quiet(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _configure_connection(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA busy_timeout=%d" % int(_env_float("SUMMARY_EXISTING_NULL_REPAIR_BUSY_TIMEOUT_MS", 30000)))
    except Exception:
        pass
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
    except Exception:
        pass
    try:
        if _env_bool("SUMMARY_EXISTING_NULL_REPAIR_FORCE_WAL", False):
            conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass


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


def _pick(cols: Iterable[str], names: Iterable[str]) -> str:
    s = set(cols or [])
    for n in names:
        if n in s:
            return n
    return ""


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


def _make_time_range(v: Any, interval: int) -> str:
    try:
        import pandas as pd
        t = pd.to_datetime(v, errors="coerce")
        if pd.isna(t):
            return "00:00:00-00:00:00"
        if hasattr(t, "to_pydatetime"):
            t = t.to_pydatetime()
        start = t.replace(second=0, microsecond=0)
        end = start + dt.timedelta(minutes=int(interval), seconds=-1)
        return f"{start.strftime('%H:%M:%S')}-{end.strftime('%H:%M:%S')}"
    except Exception:
        return "00:00:00-00:00:00"


def _load_table(conn: sqlite3.Connection, table: str, cols: list[str], limit_rows: int):
    import pandas as pd

    sym = _pick(cols, ["symbol", "code", "stock_code"])
    dtc = _pick(cols, ["datetime", "dt", "timestamp"])
    datec = _pick(cols, ["date", "trade_date"])
    timec = _pick(cols, ["time", "minute", "bar_time"])
    op = _pick(cols, ["open_price", "open"])
    hi = _pick(cols, ["high_price", "high"])
    lo = _pick(cols, ["low_price", "low"])
    cl = _pick(cols, ["close_price", "close", "price", "current_price"])
    vol = _pick(cols, ["volume", "vol"])
    turn = _pick(cols, ["turnover", "turnover_yen", "trading_value"])
    if not sym or not cl or not (dtc or (datec and timec)):
        return pd.DataFrame()

    dtexpr = dtc if dtc else f"({datec} || ' ' || {timec})"
    sql = f"""
        SELECT rowid AS _rowid,
               CAST({sym} AS TEXT) AS symbol,
               {dtexpr} AS dtv,
               {op if op else cl} AS open_price,
               {hi if hi else cl} AS high_price,
               {lo if lo else cl} AS low_price,
               {cl} AS close_price,
               {vol if vol else '0'} AS volume,
               {turn if turn else '0'} AS turnover
        FROM {table}
        ORDER BY symbol, dtv
        LIMIT ?
    """
    return pd.read_sql_query(sql, conn, params=(limit_rows,))


def _compute(df, interval: int):
    import pandas as pd
    import numpy as np

    if df is None or df.empty:
        return df
    df = df.copy()
    df["symbol"] = df["symbol"].map(_norm_symbol)
    df["dtv"] = pd.to_datetime(df["dtv"], errors="coerce")
    df = df.dropna(subset=["symbol", "dtv"])
    df = df[df["symbol"].astype(str).str.len() > 0]
    df = df.sort_values(["symbol", "dtv", "_rowid"])

    for c in ["open_price", "high_price", "low_price", "close_price", "volume", "turnover"]:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    parts = []
    for _, g in df.groupby("symbol", sort=False):
        g = g.sort_values(["dtv", "_rowid"]).copy()
        close = g["close_price"].astype(float)
        high = g["high_price"].replace(0, np.nan).fillna(close).astype(float)
        low = g["low_price"].replace(0, np.nan).fillna(close).astype(float)

        g["time_range"] = g["dtv"].map(lambda x: _make_time_range(x, interval))
        g["ma5"] = close.rolling(5, min_periods=1).mean().fillna(close)
        g["ma25"] = close.rolling(25, min_periods=1).mean().fillna(close)
        g["ma75"] = close.rolling(75, min_periods=1).mean().fillna(close)

        delta = close.diff().fillna(0.0)
        gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
        rs = gain / loss.replace(0, np.nan)
        g["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0).clip(0, 100)

        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        g["macd"] = (ema12 - ema26).fillna(0.0)
        g["signal"] = g["macd"].ewm(span=9, adjust=False, min_periods=1).mean().fillna(0.0)

        prev_close = close.shift(1)
        tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1).fillna(0.0)
        g["atr"] = tr.rolling(14, min_periods=1).mean().fillna(0.0)

        g["slope"] = close.pct_change(3).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        atr_pct = (g["atr"] / close.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        g["slope_atr_scaled"] = (g["slope"] / atr_pct.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        g["score_slope"] = (g["slope"] * 100.0).clip(-3, 3).fillna(0.0)

        ma_buy = ((g["ma5"] > g["ma25"]).astype(float) + (g["ma25"] > g["ma75"]).astype(float))
        ma_sell = ((g["ma5"] < g["ma25"]).astype(float) + (g["ma25"] < g["ma75"]).astype(float))
        macd_buy = (g["macd"] > g["signal"]).astype(float)
        macd_sell = (g["macd"] < g["signal"]).astype(float)
        rsi_buy = ((g["rsi"] >= 50) & (g["rsi"] <= 75)).astype(float)
        rsi_sell = ((g["rsi"] <= 50) & (g["rsi"] >= 25)).astype(float)
        slope_buy = (g["slope"] > 0).astype(float)
        slope_sell = (g["slope"] < 0).astype(float)

        score_buy = ma_buy + macd_buy + rsi_buy + slope_buy
        score_sell = ma_sell + macd_sell + rsi_sell + slope_sell
        g["score_buy"] = score_buy.fillna(0.0)
        g["score_sell"] = score_sell.fillna(0.0)
        g["score_total"] = (score_buy - score_sell).fillna(0.0)
        g["score"] = g["score_total"]
        g["final_score"] = g["score_total"]
        g["display_score"] = g["score_total"]
        g["score_mtf"] = (ma_buy - ma_sell).fillna(0.0)
        g["mtf_score"] = g["score_mtf"]
        g["mtf"] = g["score_mtf"]
        parts.append(g)

    return pd.concat(parts, ignore_index=True) if parts else df


def _needs_update_where(cols: list[str]) -> str:
    candidates = [
        "time_range", "rsi", "macd", "signal", "atr", "slope", "slope_atr_scaled",
        "ma5", "ma25", "ma75", "score_buy", "score_sell", "score_total", "final_score", "score_mtf", "mtf",
    ]
    terms = [f"{c} IS NULL" for c in candidates if c in cols]
    if not terms:
        return ""
    return " OR ".join(terms)


def _target_rowids_with_null(conn: sqlite3.Connection, table: str, cols: list[str]) -> set[int]:
    where = _needs_update_where(cols)
    if not where:
        return set()
    try:
        rows = conn.execute(f"SELECT rowid FROM {table} WHERE {where}").fetchall()
        return {int(r[0]) for r in rows}
    except Exception:
        logger.warning("[SUMMARY EXISTING NULL REPAIR] rowid scan failed table=%s", table, exc_info=True)
        return set()


def _update_table(conn: sqlite3.Connection, table: str, cols: list[str], calc, target_rowids: set[int]) -> int:
    if calc is None or calc.empty or not target_rowids:
        return 0
    wanted = [
        "time_range", "rsi", "macd", "signal", "atr", "slope", "slope_atr_scaled", "score_slope",
        "ma5", "ma25", "ma75", "score", "score_buy", "score_sell", "score_total",
        "final_score", "display_score", "score_mtf", "mtf_score", "mtf",
    ]
    update_cols = [c for c in wanted if c in cols and c in calc.columns]
    if "updated_at" in cols:
        calc = calc.copy()
        calc["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_cols.append("updated_at")
    if not update_cols:
        return 0

    target = calc[calc["_rowid"].astype(int).isin(target_rowids)].copy()
    if target.empty:
        return 0

    set_sql = ", ".join([f"{c}=?" for c in update_cols])
    sql = f"UPDATE {table} SET {set_sql} WHERE rowid=?"
    vals = []
    for _, r in target.iterrows():
        rowid = int(r["_rowid"])
        one = []
        for c in update_cols:
            if c == "updated_at" or c == "time_range":
                one.append(r.get(c))
            else:
                one.append(float(r.get(c, 0.0) or 0.0))
        vals.append(tuple(one) + (rowid,))

    chunk_size = max(1, _env_int("SUMMARY_EXISTING_NULL_REPAIR_CHUNK_SIZE", 300))
    retries = max(0, _env_int("SUMMARY_EXISTING_NULL_REPAIR_LOCK_RETRIES", 8))
    sleep_base = max(0.05, _env_float("SUMMARY_EXISTING_NULL_REPAIR_LOCK_SLEEP_BASE", 0.35))
    skip_if_busy = _env_bool("SUMMARY_EXISTING_NULL_REPAIR_SKIP_IF_BUSY", True)

    total = 0
    for i in range(0, len(vals), chunk_size):
        chunk = vals[i:i + chunk_size]
        attempt = 0
        while True:
            try:
                conn.executemany(sql, chunk)
                conn.commit()
                total += len(chunk)
                break
            except sqlite3.OperationalError as e:
                _rollback_quiet(conn)
                if not _is_locked_error(e):
                    raise
                attempt += 1
                if attempt > retries:
                    logger.warning("[SUMMARY EXISTING NULL REPAIR] locked giveup table=%s done=%s err=%s", table, total, e)
                    if skip_if_busy:
                        return total
                    raise
                time.sleep(sleep_base * (1.5 ** (attempt - 1)) + random.uniform(0, sleep_base))
    return total


def run_repair(*, reason: str = "manual") -> dict[str, Any]:
    global _RUNNING
    with _LOCK:
        if _RUNNING:
            return {"ok": False, "skipped": True, "error": "already_running", "reason": reason}
        _RUNNING = True
    try:
        return _run_repair_impl(reason=reason)
    finally:
        with _LOCK:
            _RUNNING = False


def _run_repair_impl(*, reason: str) -> dict[str, Any]:
    t0 = time.monotonic()
    path = _summary_db_path()
    result: dict[str, Any] = {"ok": False, "path": path, "reason": reason, "details": {}}
    if not Path(path).exists():
        result["error"] = "summary_db_not_found"
        logger.warning("[SUMMARY EXISTING NULL REPAIR] skip db not found path=%s", path)
        return result

    limit_rows = max(1000, _env_int("SUMMARY_EXISTING_NULL_REPAIR_MAX_ROWS", 300000))
    total = 0
    try:
        with sqlite3.connect(path, timeout=_env_float("SUMMARY_EXISTING_NULL_REPAIR_SQLITE_TIMEOUT", 30.0)) as conn:
            _configure_connection(conn)
            for interval in _parse_intervals():
                table = f"stock_summary_{interval}min"
                detail = {"table": table, "interval": interval}
                result["details"][interval] = detail
                if not _table_exists(conn, table):
                    detail["error"] = "table_missing"
                    continue
                cols = _columns(conn, table)
                target_rowids = _target_rowids_with_null(conn, table, cols)
                detail["null_rows"] = len(target_rowids)
                if not target_rowids:
                    detail["updated"] = 0
                    logger.warning("[SUMMARY EXISTING NULL REPAIR] interval=%s table=%s null_rows=0", interval, table)
                    continue
                df = _load_table(conn, table, cols, limit_rows)
                detail["loaded"] = int(len(df)) if df is not None else 0
                if df is None or df.empty:
                    detail["updated"] = 0
                    continue
                calc = _compute(df, interval)
                updated = _update_table(conn, table, cols, calc, target_rowids)
                detail["updated"] = updated
                total += updated
                logger.warning(
                    "[SUMMARY EXISTING NULL REPAIR] interval=%s table=%s loaded=%s null_rows=%s updated=%s",
                    interval, table, len(df), len(target_rowids), updated,
                )
        result.update({"ok": True, "updated": total, "elapsed": round(time.monotonic() - t0, 3)})
        logger.warning("[SUMMARY EXISTING NULL REPAIR] done reason=%s updated=%s elapsed=%.3fs db=%s", reason, total, time.monotonic() - t0, path)
        return result
    except Exception as e:
        result["error"] = str(e)
        logger.warning("[SUMMARY EXISTING NULL REPAIR] failed reason=%s err=%s path=%s", reason, e, path, exc_info=True)
        return result


def _run_background(reason: str) -> None:
    delay = _env_float("SUMMARY_EXISTING_NULL_REPAIR_START_DELAY_SEC", 18.0)
    if delay > 0:
        time.sleep(delay)
    run_repair(reason=reason)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_EXISTING_NULL_REPAIR_ENABLED", True):
        logger.warning("[SUMMARY EXISTING NULL REPAIR] disabled by env")
        return False
    _INSTALLED = True
    logger.warning(
        "[SUMMARY EXISTING NULL REPAIR] installed intervals=%s delay=%.1fs max_rows=%s",
        _parse_intervals(),
        _env_float("SUMMARY_EXISTING_NULL_REPAIR_START_DELAY_SEC", 18.0),
        _env_int("SUMMARY_EXISTING_NULL_REPAIR_MAX_ROWS", 300000),
    )
    th = threading.Thread(target=_run_background, args=("startup",), name="summary-existing-null-repair", daemon=True)
    th.start()
    return True


try:
    install()
except Exception as e:
    logger.warning("[SUMMARY EXISTING NULL REPAIR] auto install failed err=%s", e, exc_info=False)

__all__ = ["install", "run_repair"]
