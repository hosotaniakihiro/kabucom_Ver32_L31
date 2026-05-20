# ============================================================
# File   : core/startup/summary_mtf_indicator_fill_patch.py
# Version: V1.0-FILL-3M-5M-INDICATORS-AFTER-CATCHUP
# ------------------------------------------------------------
# summary_multiframe_startup_catchup_patch はOHLCVを作るが、
# rsi/macd/signal/ma75/score_mtf/final_score などの指標列は
# 通常サマリー計算まで0/50/NULLになりやすい。
#
# このパッチは起動時に stock_summary_3min / stock_summary_5min を読み、
# 銘柄ごとに基本テクニカルを再計算してDBへUPDATEする。
#
# 対象列がテーブルに存在する場合のみ更新する。
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
    raw = os.getenv("SUMMARY_MTF_INDICATOR_INTERVALS", "3,5")
    out: list[int] = []
    for x in str(raw).replace(";", ",").split(","):
        try:
            n = int(float(x.strip()))
            if n in (3, 5) and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [3, 5]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
    except Exception:
        return False


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


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _compute_indicators(df):
    import pandas as pd
    import numpy as np

    if df is None or df.empty:
        return df
    df = df.copy()
    df["symbol"] = df["symbol"].map(_norm_symbol)
    df["dtv"] = pd.to_datetime(df["dtv"], errors="coerce")
    df = df.dropna(subset=["symbol", "dtv"])
    df = df.sort_values(["symbol", "dtv"])

    for c in ["open_price", "high_price", "low_price", "close_price", "volume", "turnover"]:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    out_parts = []
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values("dtv").copy()
        close = g["close_price"].astype(float)
        high = g["high_price"].replace(0, np.nan).fillna(close).astype(float)
        low = g["low_price"].replace(0, np.nan).fillna(close).astype(float)

        g["ma5"] = close.rolling(5, min_periods=1).mean()
        g["ma25"] = close.rolling(25, min_periods=1).mean()
        g["ma75"] = close.rolling(75, min_periods=1).mean()

        delta = close.diff().fillna(0.0)
        gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        # 値動きゼロの初期行は50。上昇のみは100、下落のみは0へ寄せる。
        rsi = rsi.fillna(50.0)
        rsi = rsi.clip(0, 100)
        g["rsi"] = rsi

        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        macd = ema12 - ema26
        sig = macd.ewm(span=9, adjust=False, min_periods=1).mean()
        g["macd"] = macd.fillna(0.0)
        g["signal"] = sig.fillna(0.0)

        prev_close = close.shift(1)
        tr1 = (high - low).abs()
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).fillna(0.0)
        atr = tr.rolling(14, min_periods=1).mean().fillna(0.0)
        g["atr"] = atr

        slope = close.pct_change(3).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        g["slope"] = slope
        atr_pct = (atr / close.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        g["slope_atr_scaled"] = (slope / atr_pct.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        g["score_slope"] = (slope * 100.0).clip(-3, 3).fillna(0.0)

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
        # 3m/5m単独では本当のMTF統合はできないため、暫定的にMA整列強度を入れる。
        g["score_mtf"] = (ma_buy - ma_sell).fillna(0.0)
        g["mtf_score"] = g["score_mtf"]
        g["mtf"] = g["score_mtf"]
        out_parts.append(g)

    if not out_parts:
        return df
    return pd.concat(out_parts, ignore_index=True)


def _load_table_df(conn: sqlite3.Connection, table: str, cols: list[str], interval: int):
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
    lookback_bars = _env_int("SUMMARY_MTF_INDICATOR_LOOKBACK_BARS", 120)
    lookback_min = max(interval * lookback_bars + 30, interval * 75 + 30)
    since = (dt.datetime.now() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
    max_rows = _env_int("SUMMARY_MTF_INDICATOR_MAX_ROWS", 120000)
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
        WHERE {dtexpr} >= ?
        ORDER BY symbol, dtv
        LIMIT ?
    """
    return pd.read_sql_query(sql, conn, params=(since, max_rows))


def _update_table(conn: sqlite3.Connection, table: str, cols: list[str], df) -> int:
    if df is None or df.empty:
        return 0
    wanted = [
        "rsi", "macd", "signal", "atr", "slope", "slope_atr_scaled", "score_slope",
        "ma5", "ma25", "ma75",
        "score", "score_buy", "score_sell", "score_total", "final_score", "display_score",
        "score_mtf", "mtf_score", "mtf",
    ]
    update_cols = [c for c in wanted if c in cols and c in df.columns]
    if not update_cols:
        logger.warning("[SUMMARY MTF INDICATOR FILL] no writable indicator columns table=%s", table)
        return 0
    if "updated_at" in cols:
        df = df.copy()
        df["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        update_cols.append("updated_at")

    set_sql = ", ".join([f"{c}=?" for c in update_cols])
    sql = f"UPDATE {table} SET {set_sql} WHERE rowid=?"
    vals = []
    for _, r in df.iterrows():
        rowid = int(r["_rowid"])
        vals.append(tuple(_f(r.get(c), 0.0) if c != "updated_at" else r.get(c) for c in update_cols) + (rowid,))
    conn.executemany(sql, vals)
    return len(vals)


def run_fill(*, reason: str = "manual") -> dict[str, Any]:
    t0 = time.monotonic()
    path = _summary_db_path()
    result: dict[str, Any] = {"ok": False, "path": path, "reason": reason, "details": {}}
    if not Path(path).exists():
        result["error"] = "summary_db_not_found"
        logger.warning("[SUMMARY MTF INDICATOR FILL] skip db not found path=%s", path)
        return result
    try:
        total = 0
        with sqlite3.connect(path, timeout=_env_float("SUMMARY_MTF_INDICATOR_SQLITE_TIMEOUT", 5.0)) as conn:
            conn.execute("PRAGMA busy_timeout=%d" % int(_env_float("SUMMARY_MTF_INDICATOR_BUSY_TIMEOUT_MS", 5000)))
            for interval in _parse_intervals():
                table = f"stock_summary_{interval}min"
                detail: dict[str, Any] = {"interval": interval, "table": table}
                result["details"][interval] = detail
                if not _table_exists(conn, table):
                    detail["error"] = "table_missing"
                    continue
                cols = _columns(conn, table)
                df = _load_table_df(conn, table, cols, interval)
                detail["loaded"] = int(len(df)) if df is not None else 0
                if df is None or df.empty:
                    detail["updated"] = 0
                    continue
                calc = _compute_indicators(df)
                updated = _update_table(conn, table, cols, calc)
                total += updated
                detail["updated"] = updated
                logger.warning(
                    "[SUMMARY MTF INDICATOR FILL] interval=%s loaded=%s updated=%s table=%s",
                    interval, len(df), updated, table,
                )
            conn.commit()
        result.update({"ok": True, "updated": total, "elapsed": round(time.monotonic() - t0, 3)})
        logger.warning("[SUMMARY MTF INDICATOR FILL] done reason=%s updated=%s elapsed=%.3fs db=%s", reason, total, time.monotonic() - t0, path)
        return result
    except Exception as e:
        result["error"] = str(e)
        logger.warning("[SUMMARY MTF INDICATOR FILL] failed reason=%s err=%s path=%s", reason, e, path, exc_info=True)
        return result


def _run_background(reason: str) -> None:
    global _RUNNING
    delay = _env_float("SUMMARY_MTF_INDICATOR_START_DELAY_SEC", 4.0)
    if delay > 0:
        time.sleep(delay)
    with _LOCK:
        if _RUNNING:
            logger.info("[SUMMARY MTF INDICATOR FILL] already running skip reason=%s", reason)
            return
        _RUNNING = True
    try:
        run_fill(reason=reason)
    finally:
        with _LOCK:
            _RUNNING = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_MTF_INDICATOR_FILL_ENABLED", True):
        logger.warning("[SUMMARY MTF INDICATOR FILL] disabled by env")
        return False
    _INSTALLED = True
    logger.warning(
        "[SUMMARY MTF INDICATOR FILL] installed intervals=%s delay=%.1fs",
        _parse_intervals(), _env_float("SUMMARY_MTF_INDICATOR_START_DELAY_SEC", 4.0),
    )
    th = threading.Thread(target=_run_background, args=("startup",), name="summary-mtf-indicator-fill", daemon=True)
    th.start()
    return True

try:
    install()
except Exception as e:
    logger.warning("[SUMMARY MTF INDICATOR FILL] auto install failed err=%s", e, exc_info=False)

__all__ = ["install", "run_fill"]
