# ============================================================
# File   : core/startup/summary_mtf_indicator_fill_patch.py
# Version: V2.0-FILL-1M-3M-5M-WITH-HISTORY
# ------------------------------------------------------------
# summary_multiframe_startup_catchup_patch はOHLCVを作るが、
# rsi/macd/signal/ma75/score_mtf/final_score などの指標列は
# 通常サマリー計算まで0/50/NULLになりやすい。
#
# このパッチは起動時に stock_summary_1min / 3min / 5min を読み、
# 銘柄ごとに基本テクニカルを再計算してDBへUPDATEする。
#
# V2.0:
#   - 1分足 stock_summary_1min も補完対象に追加
#   - 当日DBだけでなく、過去 summaryYYYYMMDD.db を先頭に結合してから計算
#   - 計算は「過去足 + 当日足」で行い、UPDATEは当日DBのrowidだけに限定
#   - 途中からランキング/監視に入った銘柄でも rsi/macd/signal/ma75 の先頭NULLを減らす
#   - NAS SQLite の database is locked 対策、chunk commit、skip_if_busy は維持
#
# 主な環境変数:
#   SUMMARY_MTF_INDICATOR_INTERVALS=1,3,5
#   SUMMARY_MTF_INDICATOR_HISTORY_DAYS=7
#   SUMMARY_MTF_INDICATOR_LOOKBACK_BARS=180
#   SUMMARY_MTF_INDICATOR_MAX_ROWS=250000
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import random
import re
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


def _extract_yyyymmdd(path: str) -> str:
    try:
        m = re.search(r"summary(\d{8})\.db$", str(path))
        return m.group(1) if m else ""
    except Exception:
        return ""


def _summary_db_paths_with_history(current_path: str) -> list[str]:
    """current_path を最後に置き、前日以前のsummary DBを先に返す。

    指標計算には過去足が必要だが、UPDATE対象は current_path のrowidだけにする。
    土日/祝日を厳密判定せず、指定日数ぶん暦日で戻って存在するDBのみ採用する。
    """
    cur = Path(current_path)
    out: list[str] = []
    history_days = max(0, _env_int("SUMMARY_MTF_INDICATOR_HISTORY_DAYS", 7))
    ymd = _extract_yyyymmdd(current_path)
    if not ymd:
        return [current_path]
    try:
        base_date = dt.datetime.strptime(ymd, "%Y%m%d").date()
    except Exception:
        return [current_path]

    for i in range(history_days, 0, -1):
        d = base_date - dt.timedelta(days=i)
        p = cur.with_name(f"summary{d.strftime('%Y%m%d')}.db")
        if p.exists():
            out.append(str(p))
    out.append(str(cur))
    # 念のため重複排除
    uniq: list[str] = []
    seen: set[str] = set()
    for p in out:
        key = os.path.abspath(p).lower()
        if key not in seen:
            uniq.append(p)
            seen.add(key)
    return uniq


def _parse_intervals() -> list[int]:
    raw = os.getenv("SUMMARY_MTF_INDICATOR_INTERVALS", "1,3,5")
    out: list[int] = []
    for x in str(raw).replace(";", ",").split(","):
        try:
            n = int(float(x.strip()))
            if n in (1, 3, 5) and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [1, 3, 5]


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


def _is_locked_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return "database is locked" in msg or "database table is locked" in msg or "database busy" in msg or "locked" in msg


def _rollback_quiet(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _configure_connection(conn: sqlite3.Connection) -> None:
    """NAS SQLite向けの軽い接続設定。失敗しても本処理は継続する。"""
    try:
        conn.execute("PRAGMA busy_timeout=%d" % int(_env_float("SUMMARY_MTF_INDICATOR_BUSY_TIMEOUT_MS", 30000)))
    except Exception:
        pass
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
    except Exception:
        pass
    try:
        # WALはNAS環境で環境差があるため、既にWALなら維持する程度に留める。
        if _env_bool("SUMMARY_MTF_INDICATOR_FORCE_WAL", False):
            conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass


def _compute_indicators(df):
    import pandas as pd
    import numpy as np

    if df is None or df.empty:
        return df
    df = df.copy()
    df["symbol"] = df["symbol"].map(_norm_symbol)
    df["dtv"] = pd.to_datetime(df["dtv"], errors="coerce")
    df = df.dropna(subset=["symbol", "dtv"])
    df = df[df["symbol"].astype(str).str.len() > 0]
    df = df.sort_values(["symbol", "dtv", "_is_target", "_rowid"], na_position="first")

    for c in ["open_price", "high_price", "low_price", "close_price", "volume", "turnover"]:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    out_parts = []
    for sym, g in df.groupby("symbol", sort=False):
        g = g.sort_values(["dtv", "_is_target", "_rowid"], na_position="first").copy()
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
        rsi = rsi.fillna(50.0).clip(0, 100)
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
        g["score_mtf"] = (ma_buy - ma_sell).fillna(0.0)
        g["mtf_score"] = g["score_mtf"]
        g["mtf"] = g["score_mtf"]
        out_parts.append(g)

    if not out_parts:
        return df
    return pd.concat(out_parts, ignore_index=True)


def _build_select_sql(table: str, cols: list[str], *, current_db: bool, interval: int) -> tuple[str, tuple[Any, ...]]:
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
        return "", ()

    dtexpr = dtc if dtc else f"({datec} || ' ' || {timec})"
    lookback_bars = _env_int("SUMMARY_MTF_INDICATOR_LOOKBACK_BARS", 180)
    # 75MA / MACD signal / 5分足75本を考慮して、多めに読む
    lookback_min = max(interval * lookback_bars + 30, interval * 90 + 30)
    since = (dt.datetime.now() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
    max_rows = _env_int("SUMMARY_MTF_INDICATOR_MAX_ROWS", 250000)

    rowid_expr = "rowid" if current_db else "NULL"
    target_expr = "1" if current_db else "0"
    sql = f"""
        SELECT {rowid_expr} AS _rowid,
               {target_expr} AS _is_target,
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
    return sql, (since, max_rows)


def _load_one_table_df(path: str, table: str, interval: int, *, current_db: bool):
    import pandas as pd

    if not Path(path).exists():
        return pd.DataFrame()
    try:
        timeout = _env_float("SUMMARY_MTF_INDICATOR_SQLITE_TIMEOUT", 30.0)
        with sqlite3.connect(path, timeout=timeout) as conn:
            _configure_connection(conn)
            if not _table_exists(conn, table):
                return pd.DataFrame()
            cols = _columns(conn, table)
            sql, params = _build_select_sql(table, cols, current_db=current_db, interval=interval)
            if not sql:
                return pd.DataFrame()
            df = pd.read_sql_query(sql, conn, params=params)
            df["_src_db"] = Path(path).name
            return df
    except sqlite3.OperationalError as e:
        if _is_locked_error(e) and not current_db:
            logger.warning("[SUMMARY MTF INDICATOR FILL] history db locked skip path=%s table=%s err=%s", path, table, e, exc_info=False)
            return pd.DataFrame()
        raise
    except Exception as e:
        logger.warning("[SUMMARY MTF INDICATOR FILL] load table failed path=%s table=%s err=%s", path, table, e, exc_info=False)
        return pd.DataFrame()


def _load_table_df_with_history(current_path: str, table: str, interval: int):
    import pandas as pd

    frames = []
    paths = _summary_db_paths_with_history(current_path)
    for p in paths:
        current_db = os.path.abspath(p).lower() == os.path.abspath(current_path).lower()
        dfp = _load_one_table_df(p, table, interval, current_db=current_db)
        if dfp is not None and not dfp.empty:
            frames.append(dfp)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)

    # 同一 symbol/dtv が過去DBと当日DBに重複した場合は当日DBを優先
    try:
        df["_sym_norm"] = df["symbol"].map(_norm_symbol)
        df["dtv"] = pd.to_datetime(df["dtv"], errors="coerce")
        df = df.sort_values(["_sym_norm", "dtv", "_is_target"], na_position="first")
        df = df.drop_duplicates(subset=["_sym_norm", "dtv"], keep="last")
        df = df.drop(columns=["_sym_norm"], errors="ignore")
    except Exception:
        pass
    return df


def _update_table(conn: sqlite3.Connection, table: str, cols: list[str], df) -> int:
    if df is None or df.empty:
        return 0

    # UPDATE対象は当日DBのrowidを持つ行だけ
    try:
        df = df[(df.get("_is_target", 0).astype(int) == 1) & df["_rowid"].notna()].copy()
    except Exception:
        df = df[df["_rowid"].notna()].copy()

    if df.empty:
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

    if not vals:
        return 0

    chunk_size = max(1, _env_int("SUMMARY_MTF_INDICATOR_UPDATE_CHUNK_SIZE", 200))
    max_retries = max(0, _env_int("SUMMARY_MTF_INDICATOR_LOCK_RETRIES", 8))
    sleep_base = max(0.05, _env_float("SUMMARY_MTF_INDICATOR_LOCK_SLEEP_BASE", 0.35))
    skip_if_busy = _env_bool("SUMMARY_MTF_INDICATOR_SKIP_IF_BUSY", True)

    total_done = 0
    chunks = [vals[i:i + chunk_size] for i in range(0, len(vals), chunk_size)]
    for idx, chunk in enumerate(chunks, start=1):
        attempt = 0
        while True:
            try:
                conn.executemany(sql, chunk)
                conn.commit()
                total_done += len(chunk)
                if idx == 1 or idx == len(chunks) or idx % 10 == 0:
                    logger.info(
                        "[SUMMARY MTF INDICATOR FILL] chunk ok table=%s chunk=%s/%s rows=%s total_done=%s",
                        table, idx, len(chunks), len(chunk), total_done,
                    )
                break
            except sqlite3.OperationalError as e:
                _rollback_quiet(conn)
                if not _is_locked_error(e):
                    raise
                attempt += 1
                if attempt > max_retries:
                    logger.warning(
                        "[SUMMARY MTF INDICATOR FILL] chunk locked giveup table=%s chunk=%s/%s rows=%s done=%s retries=%s skip_if_busy=%s err=%s",
                        table, idx, len(chunks), len(chunk), total_done, max_retries, skip_if_busy, e,
                        exc_info=False,
                    )
                    if skip_if_busy:
                        return total_done
                    raise
                sleep_sec = sleep_base * (1.5 ** (attempt - 1)) + random.uniform(0.0, sleep_base)
                logger.warning(
                    "[SUMMARY MTF INDICATOR FILL] chunk locked retry table=%s chunk=%s/%s attempt=%s/%s sleep=%.2fs err=%s",
                    table, idx, len(chunks), attempt, max_retries, sleep_sec, e,
                    exc_info=False,
                )
                time.sleep(sleep_sec)
    return total_done


def _null_stats(df) -> dict[str, int]:
    try:
        target = df[(df.get("_is_target", 0).astype(int) == 1) & df["_rowid"].notna()]
        return {
            "target_rows": int(len(target)),
            "null_rsi": int(target["rsi"].isna().sum()) if "rsi" in target.columns else -1,
            "null_macd": int(target["macd"].isna().sum()) if "macd" in target.columns else -1,
            "null_signal": int(target["signal"].isna().sum()) if "signal" in target.columns else -1,
        }
    except Exception:
        return {}


def _run_fill_impl(*, reason: str = "manual") -> dict[str, Any]:
    t0 = time.monotonic()
    path = _summary_db_path()
    result: dict[str, Any] = {"ok": False, "path": path, "reason": reason, "details": {}}
    if not Path(path).exists():
        result["error"] = "summary_db_not_found"
        logger.warning("[SUMMARY MTF INDICATOR FILL] skip db not found path=%s", path)
        return result
    try:
        total = 0
        timeout = _env_float("SUMMARY_MTF_INDICATOR_SQLITE_TIMEOUT", 30.0)
        history_paths = _summary_db_paths_with_history(path)
        logger.warning(
            "[SUMMARY MTF INDICATOR FILL] start reason=%s intervals=%s history_dbs=%s current=%s",
            reason, _parse_intervals(), [Path(p).name for p in history_paths], path,
        )
        with sqlite3.connect(path, timeout=timeout) as conn:
            _configure_connection(conn)
            for interval in _parse_intervals():
                table = f"stock_summary_{interval}min"
                detail: dict[str, Any] = {"interval": interval, "table": table}
                result["details"][interval] = detail
                if not _table_exists(conn, table):
                    detail["error"] = "table_missing"
                    continue

                cols = _columns(conn, table)
                df = _load_table_df_with_history(path, table, interval)
                detail["loaded"] = int(len(df)) if df is not None else 0
                detail["history_dbs"] = len(history_paths)
                if df is None or df.empty:
                    detail["updated"] = 0
                    continue

                calc = _compute_indicators(df)
                detail["after_calc_null_stats"] = _null_stats(calc)
                updated = _update_table(conn, table, cols, calc)
                total += updated
                detail["updated"] = updated
                logger.warning(
                    "[SUMMARY MTF INDICATOR FILL] interval=%s loaded=%s updated=%s table=%s null_stats=%s",
                    interval, len(df), updated, table, detail.get("after_calc_null_stats"),
                )

        result.update({"ok": True, "updated": total, "elapsed": round(time.monotonic() - t0, 3)})
        logger.warning("[SUMMARY MTF INDICATOR FILL] done reason=%s updated=%s elapsed=%.3fs db=%s", reason, total, time.monotonic() - t0, path)
        return result
    except Exception as e:
        result["error"] = str(e)
        logger.warning("[SUMMARY MTF INDICATOR FILL] failed reason=%s err=%s path=%s", reason, e, path, exc_info=True)
        return result


def run_fill(*, reason: str = "manual") -> dict[str, Any]:
    """MTF指標補完の公開入口。

    background startup と after_mtf_catchup 等が同時に走る可能性がある。
    NAS上のSQLiteでは同時UPDATEが locked の主因になるため、入口で直列化する。
    """
    global _RUNNING
    with _LOCK:
        if _RUNNING:
            path = _summary_db_path()
            logger.info("[SUMMARY MTF INDICATOR FILL] already running skip reason=%s path=%s", reason, path)
            return {"ok": False, "skipped": True, "error": "already_running", "reason": reason, "path": path}
        _RUNNING = True
    try:
        return _run_fill_impl(reason=reason)
    finally:
        with _LOCK:
            _RUNNING = False


def _run_background(reason: str) -> None:
    delay = _env_float("SUMMARY_MTF_INDICATOR_START_DELAY_SEC", 4.0)
    if delay > 0:
        time.sleep(delay)
    run_fill(reason=reason)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_MTF_INDICATOR_FILL_ENABLED", True):
        logger.warning("[SUMMARY MTF INDICATOR FILL] disabled by env")
        return False
    _INSTALLED = True
    logger.warning(
        "[SUMMARY MTF INDICATOR FILL] installed intervals=%s delay=%.1fs history_days=%s chunk=%s retries=%s skip_if_busy=%s",
        _parse_intervals(),
        _env_float("SUMMARY_MTF_INDICATOR_START_DELAY_SEC", 4.0),
        _env_int("SUMMARY_MTF_INDICATOR_HISTORY_DAYS", 7),
        _env_int("SUMMARY_MTF_INDICATOR_UPDATE_CHUNK_SIZE", 200),
        _env_int("SUMMARY_MTF_INDICATOR_LOCK_RETRIES", 8),
        _env_bool("SUMMARY_MTF_INDICATOR_SKIP_IF_BUSY", True),
    )
    th = threading.Thread(target=_run_background, args=("startup",), name="summary-mtf-indicator-fill", daemon=True)
    th.start()
    return True


try:
    install()
except Exception as e:
    logger.warning("[SUMMARY MTF INDICATOR FILL] auto install failed err=%s", e, exc_info=False)

__all__ = ["install", "run_fill"]
