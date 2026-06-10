# ============================================================
# File   : core/startup/summary_db_seed_restore_patch.py
# Version: V1.6-SKIP-IN-MAIN-PROCESS
# ------------------------------------------------------------
# 目的:
#   main.py は split mode / entry_only のため summary DB へ正式保存しない。
#   エントリー判定・AI判定・Discord表示用の summary seed は、
#   main_database.py / DB owner 側だけで実行する。
#
# V1.6:
#   - main.py では restore_summary_db_seed() を完全スキップする。
#   - 旧V1.5の async restore でも 270〜458秒走り、summary DB lock と
#     fallback/rebuild 遅延を誘発していたため、main process では無効化。
#   - force=True または SUMMARY_DB_SEED_RESTORE_RUN_IN_MAIN=1 の場合のみ main.py でも実行可能。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_TABLES = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

_RESTORED = False
_ASYNC_LOCK = threading.Lock()
_ASYNC_THREAD: threading.Thread | None = None
_ASYNC_STARTED_AT: float | None = None
_ASYNC_SEQ = 0
_ASYNC_ACTIVE = threading.local()


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _is_main_py_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in argv:
            return False
        if any(x in argv for x in (
            "db_prepare_runner.py",
            "ranking_collector_runner.py",
            "push_receiver_runner.py",
            "yahoo_complement_runner.py",
            "summary_database_runner.py",
            "data_collectors_runner.py",
        )):
            return False
        if any(os.getenv(k) == "1" for k in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
        )):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _main_seed_restore_disabled(force: bool = False) -> bool:
    if force:
        return False
    if not _is_main_py_context():
        return False
    if _env_bool("SUMMARY_DB_SEED_RESTORE_RUN_IN_MAIN", False):
        return False
    return _env_bool("SUMMARY_DB_SEED_RESTORE_SKIP_IN_MAIN", True)


def _async_alive() -> bool:
    try:
        return _ASYNC_THREAD is not None and _ASYNC_THREAD.is_alive()
    except Exception:
        return False


def _async_age() -> float:
    try:
        if _ASYNC_STARTED_AT is None:
            return 0.0
        return max(0.0, time.time() - float(_ASYNC_STARTED_AT))
    except Exception:
        return 0.0


def _today() -> dt.date:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive().date()
    except Exception:
        return dt.datetime.now().date()


def _engine_database_path() -> Optional[str]:
    try:
        import database.session as ds

        engine = getattr(ds, "summary_engine", None) or getattr(ds, "_summary_engine", None)
        if engine is None:
            return None

        db = getattr(getattr(engine, "url", None), "database", None)
        if db:
            return str(db)

        url = str(getattr(engine, "url", "") or "")
        if url.startswith("sqlite:///"):
            return url.replace("sqlite:///", "", 1)
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] resolve summary_engine database failed")
    return None


def _resolve_db_path(db_path: Any = None) -> Optional[Path]:
    try:
        if db_path:
            return Path(str(db_path))

        env_path = os.getenv("SUMMARY_DB_SEED_RESTORE_PATH") or os.getenv("SUMMARY_DB_PATH")
        if env_path:
            return Path(env_path)

        engine_path = _engine_database_path()
        if engine_path:
            return Path(engine_path)
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] db path resolve failed")
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        cur = conn.execute(f'PRAGMA table_info("{table}")')
        return [str(row[1]) for row in cur.fetchall()]
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] PRAGMA table_info failed table=%s", table)
        return []


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _parse_summary_date(path: Path) -> Optional[dt.date]:
    try:
        m = re.search(r"summary(\d{8})\.db$", str(path.name))
        if not m:
            return None
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except Exception:
        return None


def _target_date_for_current_path(path: Path) -> Optional[dt.date]:
    return _parse_summary_date(path) or _today()


def _count_summary_rows(path: Path) -> int:
    if path is None or not path.exists():
        return 0
    total = 0
    try:
        with sqlite3.connect(str(path), timeout=5) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass
            for table in _TABLES.values():
                if not _table_exists(conn, table):
                    continue
                try:
                    cur = conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}")
                    row = cur.fetchone()
                    total += int(row[0]) if row and row[0] is not None else 0
                except Exception:
                    logger.debug("[SUMMARY DB SEED RESTORE] count failed table=%s path=%s", table, path, exc_info=True)
        return int(total)
    except Exception as e:
        logger.warning("[SUMMARY DB SEED RESTORE] summary row count failed path=%s err=%s", path, e)
        return 0


def _resolve_previous_seed_db_path(current_path: Path) -> Optional[Path]:
    if not _env_bool("SUMMARY_DB_SEED_RESTORE_PREV_FALLBACK", True):
        return None
    if current_path is None:
        return None

    cur_date = _parse_summary_date(current_path)
    if cur_date is None:
        return None

    max_days = max(1, _env_int("SUMMARY_DB_SEED_RESTORE_PREV_LOOKBACK_DAYS", 10))
    summary_dir = current_path.parent
    for i in range(1, max_days + 1):
        d = cur_date - dt.timedelta(days=i)
        p = summary_dir / f"summary{d:%Y%m%d}.db"
        if not p.exists():
            continue
        rows = _count_summary_rows(p)
        logger.warning("[SUMMARY DB SEED RESTORE][PREV CANDIDATE] i=%s path=%s rows=%s", i, p, rows)
        if rows > 0:
            return p
    return None


def _read_table(conn: sqlite3.Connection, table: str, *, per_symbol_rows: int, max_rows: int, target_date: Optional[dt.date] = None) -> pd.DataFrame:
    if not _table_exists(conn, table):
        logger.warning("[SUMMARY DB SEED RESTORE] table missing table=%s", table)
        return pd.DataFrame()

    cols = _table_columns(conn, table)
    if not cols:
        return pd.DataFrame()

    q_table = _quote_ident(table)
    order_col = None
    for cand in ("datetime", "end_time", "start_time", "time"):
        if cand in cols:
            order_col = cand
            break

    symbol_col = "symbol" if "symbol" in cols else None
    per_symbol_rows = max(1, int(per_symbol_rows))
    max_rows = max(1000, int(max_rows))

    date_filter_sql = ""
    params_prefix: list[Any] = []
    if target_date is not None and order_col:
        date_filter_sql = f" AND substr(CAST({_quote_ident(order_col)} AS TEXT),1,10) = ?"
        params_prefix.append(target_date.isoformat())

    if symbol_col and order_col:
        q_symbol = _quote_ident(symbol_col)
        q_order = _quote_ident(order_col)
        sql = f"""
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY {q_symbol}
                    ORDER BY {q_order} DESC
                ) AS __seed_rn
            FROM {q_table}
            WHERE {q_symbol} IS NOT NULL
              AND TRIM(CAST({q_symbol} AS TEXT)) <> ''
              AND {q_order} IS NOT NULL
              {date_filter_sql}
        )
        SELECT *
        FROM ranked
        WHERE __seed_rn <= ?
        ORDER BY {q_symbol} ASC, {q_order} ASC
        LIMIT ?
        """
        try:
            return pd.read_sql_query(sql, conn, params=tuple(params_prefix + [per_symbol_rows, max_rows]))
        except Exception:
            logger.exception(
                "[SUMMARY DB SEED RESTORE] per-symbol read_sql failed table=%s per_symbol_rows=%s target_date=%s -> fallback global limit",
                table,
                per_symbol_rows,
                target_date,
            )

    if order_col:
        q_order = _quote_ident(order_col)
        sql = f"SELECT * FROM (SELECT * FROM {q_table} WHERE {q_order} IS NOT NULL {date_filter_sql} ORDER BY {q_order} DESC LIMIT ?) ORDER BY {q_order} ASC"
        params = tuple(params_prefix + [max_rows])
    else:
        sql = f"SELECT * FROM {q_table} LIMIT ?"
        params = (max_rows,)

    try:
        return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] read_sql failed table=%s", table)
        return pd.DataFrame()


def _filter_df_to_target_date(df: pd.DataFrame, *, target_date: Optional[dt.date], interval: int, context: str) -> pd.DataFrame:
    if df is None or df.empty or target_date is None or "datetime" not in df.columns:
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    out = df.copy()
    dts = pd.to_datetime(out["datetime"], errors="coerce")
    mask = dts.notna() & (dts.dt.date == target_date)
    before = len(out)
    wrong = int((dts.notna() & ~mask).sum())
    invalid = int((~dts.notna()).sum())
    if wrong or invalid:
        logger.warning(
            "[SUMMARY DB SEED RESTORE] date filtered interval=%s context=%s target_date=%s before=%s after=%s wrong_date=%s invalid_dt=%s",
            interval,
            context,
            target_date,
            before,
            int(mask.sum()),
            wrong,
            invalid,
        )
    return out.loc[mask].copy().reset_index(drop=True)


def _normalize_summary_df(df: pd.DataFrame, interval: int, *, target_date: Optional[dt.date] = None, context: str = "") -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()
        out = df.copy()
        out = out.loc[:, ~out.columns.duplicated()].copy()
        if "__seed_rn" in out.columns:
            out = out.drop(columns=["__seed_rn"], errors="ignore")
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        else:
            return pd.DataFrame()
        for canonical, alias in (("open", "open_price"), ("high", "high_price"), ("low", "low_price"), ("close", "close_price")):
            if canonical not in out.columns and alias in out.columns:
                out[canonical] = out[alias]
            if alias not in out.columns and canonical in out.columns:
                out[alias] = out[canonical]
        if "close" not in out.columns and "price" in out.columns:
            out["close"] = out["price"]
            if "close_price" not in out.columns:
                out["close_price"] = out["price"]
        if "datetime" not in out.columns:
            if "end_time" in out.columns:
                out["datetime"] = out["end_time"]
            elif "start_time" in out.columns:
                out["datetime"] = out["start_time"]
        if "source" not in out.columns:
            out["source"] = "push"
        else:
            out["source"] = out["source"].fillna("push").astype(str).str.strip().replace({"": "push"})
        out["interval"] = interval
        for col in (
            "score", "score_total", "final_score", "display_score", "score_buy", "score_sell",
            "slope", "slope_atr_scaled", "score_slope", "mtf", "score_mtf", "mtf_score",
            "open", "high", "low", "close", "open_price", "high_price", "low_price", "close_price",
            "volume", "trading_value", "turnover", "rsi", "macd", "signal", "ma5", "ma25", "ma75",
        ):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.dropna(subset=["datetime"])
            out = _filter_df_to_target_date(out, target_date=target_date, interval=interval, context=context)
            if out.empty:
                return pd.DataFrame()
            out = out.sort_values(["symbol", "datetime"], kind="stable")
            out = out.drop_duplicates(subset=["symbol", "datetime", "source"], keep="last")
        else:
            out = out.drop_duplicates(subset=["symbol", "source"], keep="last")
        out = out[out["symbol"].astype(str).str.strip() != ""].reset_index(drop=True)
        return out
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] normalize failed interval=%s", interval)
        return pd.DataFrame()


def _seed_is_stale_for_merged(df: pd.DataFrame) -> tuple[bool, str]:
    try:
        if not _env_bool("SUMMARY_DB_SEED_RESTORE_HISTORY_ONLY_FOR_STALE", True):
            return False, "disabled"
        if df is None or df.empty or "datetime" not in df.columns:
            return False, "no_datetime"
        dt_ser = pd.to_datetime(df["datetime"], errors="coerce")
        latest = dt_ser.max()
        if pd.isna(latest):
            return False, "latest_na"
        today = _today()
        if latest.date() < today:
            return True, f"latest_date={latest.date()}<today={today}"
        return False, f"latest_date={latest.date()}"
    except Exception:
        logger.debug("[SUMMARY DB SEED RESTORE] stale merged check failed", exc_info=True)
        return False, "check_failed"


def _publish_interval(interval: int, df: pd.DataFrame) -> dict[str, Any]:
    stats: dict[str, Any] = {"interval": interval, "rows": 0, "push_rows": 0, "ranking_rows": 0}
    if df is None or df.empty:
        return stats
    try:
        from global_state import global_data
        from core.global_context.context import global_context as GC
        try:
            GC.set_summary_history(interval, df, source="db_seed")
        except Exception:
            logger.exception("[SUMMARY DB SEED RESTORE] set_summary_history failed interval=%s", interval)
        try:
            setattr(global_data, f"summary_{interval}m_df", df.copy())
        except Exception:
            pass
        stale_for_merged, stale_reason = _seed_is_stale_for_merged(df)
        if stale_for_merged:
            logger.warning(
                "[SUMMARY DB SEED RESTORE] history-only seed interval=%s reason=%s rows=%s; skip set_push/ranking_merged_summary",
                interval,
                stale_reason,
                len(df),
            )
            push_df = pd.DataFrame()
            ranking_df = pd.DataFrame()
        else:
            src = df["source"].fillna("push").astype(str).str.lower() if "source" in df.columns else pd.Series("push", index=df.index)
            ranking_mask = src.str.contains("ranking|rank", regex=True, na=False)
            push_mask = ~ranking_mask
            push_df = df.loc[push_mask].copy()
            ranking_df = df.loc[ranking_mask].copy()
            if not push_df.empty:
                try:
                    global_data.set_push_merged_summary(interval, push_df)
                except Exception:
                    logger.exception("[SUMMARY DB SEED RESTORE] set_push_merged_summary failed interval=%s", interval)
            if not ranking_df.empty:
                try:
                    global_data.set_ranking_merged_summary(interval, ranking_df)
                except Exception:
                    logger.exception("[SUMMARY DB SEED RESTORE] set_ranking_merged_summary failed interval=%s", interval)
        stats.update(
            rows=int(len(df)),
            push_rows=int(len(push_df)),
            ranking_rows=int(len(ranking_df)),
            merged_skipped_stale=int(bool(stale_for_merged)),
            merged_skip_reason=stale_reason,
            symbols=int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
            latest_dt=str(pd.to_datetime(df["datetime"], errors="coerce").max()) if "datetime" in df.columns else None,
            min_dt=str(pd.to_datetime(df["datetime"], errors="coerce").min()) if "datetime" in df.columns else None,
            max_rows_per_symbol=int(df.groupby("symbol").size().max()) if "symbol" in df.columns and len(df) else 0,
            macd_nonzero=int((pd.to_numeric(df.get("macd"), errors="coerce").fillna(0) != 0).sum()) if "macd" in df.columns else -1,
            signal_nonzero=int((pd.to_numeric(df.get("signal"), errors="coerce").fillna(0) != 0).sum()) if "signal" in df.columns else -1,
            mtf_nonzero=int((pd.to_numeric(df.get("mtf"), errors="coerce").fillna(0) != 0).sum()) if "mtf" in df.columns else -1,
        )
        return stats
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] publish failed interval=%s", interval)
        return stats


def _read_all_intervals_from_path(path: Path, *, per_symbol_rows: int, max_rows: int, target_date: Optional[dt.date]) -> dict[int, pd.DataFrame]:
    out: dict[int, pd.DataFrame] = {}
    with sqlite3.connect(str(path), timeout=15) as conn:
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA query_only=ON")
        except Exception:
            pass
        for interval, table in _TABLES.items():
            raw = _read_table(conn, table, per_symbol_rows=per_symbol_rows, max_rows=max_rows, target_date=target_date)
            out[interval] = _normalize_summary_df(raw, interval=interval, target_date=target_date, context=str(path))
    return out


def _build_result_from_frames(path: Path, frames: dict[int, pd.DataFrame], *, per_symbol_rows: int, max_rows: int, target_date: Optional[dt.date]) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "path": str(path), "target_date": str(target_date) if target_date else None, "per_symbol_rows": int(per_symbol_rows), "max_rows": int(max_rows), "intervals": {}}
    for interval, table in _TABLES.items():
        df = frames.get(interval, pd.DataFrame())
        stats = _publish_interval(interval, df)
        stats["table"] = table
        stats["raw_rows"] = int(len(df)) if isinstance(df, pd.DataFrame) else 0
        result["intervals"][str(interval)] = stats
        logger.warning(
            "[SUMMARY DB SEED RESTORE] interval=%sm table=%s raw_rows=%s rows=%s symbols=%s max_rows_per_symbol=%s push_rows=%s ranking_rows=%s merged_skipped_stale=%s min_dt=%s latest_dt=%s target_date=%s macd_nonzero=%s signal_nonzero=%s mtf_nonzero=%s",
            interval,
            table,
            stats.get("raw_rows"),
            stats.get("rows"),
            stats.get("symbols"),
            stats.get("max_rows_per_symbol"),
            stats.get("push_rows"),
            stats.get("ranking_rows"),
            stats.get("merged_skipped_stale"),
            stats.get("min_dt"),
            stats.get("latest_dt"),
            target_date,
            stats.get("macd_nonzero"),
            stats.get("signal_nonzero"),
            stats.get("mtf_nonzero"),
        )
    return result


def _restore_from_path(path: Path, *, per_symbol_rows: int, max_rows: int, target_date: Optional[dt.date]) -> dict[str, Any]:
    logger.warning("[SUMMARY DB SEED RESTORE] start path=%s per_symbol_rows=%s max_rows=%s target_date=%s", path, per_symbol_rows, max_rows, target_date)
    frames = _read_all_intervals_from_path(path, per_symbol_rows=per_symbol_rows, max_rows=max_rows, target_date=target_date)
    return _build_result_from_frames(path, frames, per_symbol_rows=per_symbol_rows, max_rows=max_rows, target_date=target_date)


def _total_restored_rows(result: dict[str, Any]) -> int:
    try:
        intervals = result.get("intervals") or {}
        return int(sum(int(v.get("rows") or 0) for v in intervals.values() if isinstance(v, dict)))
    except Exception:
        return 0


def _result_needs_previous_seed(result: dict[str, Any], *, per_symbol_rows: int) -> tuple[bool, str]:
    if not _env_bool("SUMMARY_DB_SEED_RESTORE_USE_PREV_IF_INSUFFICIENT", True):
        return False, "disabled"
    if _total_restored_rows(result) <= 0:
        return True, "current_empty"
    intervals = result.get("intervals") or {}
    if not intervals:
        return True, "no_intervals"
    min_bars = max(1, _env_int("SUMMARY_DB_SEED_RESTORE_MIN_CURRENT_BARS_PER_SYMBOL", min(30, int(per_symbol_rows))))
    weak = []
    macd_total = 0
    signal_total = 0
    for key in ("1", "3", "5"):
        st = intervals.get(key) or {}
        max_rows_per_symbol = int(st.get("max_rows_per_symbol") or 0)
        rows = int(st.get("rows") or 0)
        macd_total += max(0, int(st.get("macd_nonzero") or 0))
        signal_total += max(0, int(st.get("signal_nonzero") or 0))
        if rows <= 0 or max_rows_per_symbol < min_bars:
            weak.append(f"{key}m:max_rows_per_symbol={max_rows_per_symbol}<min={min_bars}")
    if weak:
        return True, ";".join(weak)
    if _env_bool("SUMMARY_DB_SEED_RESTORE_REQUIRE_MACD_SIGNAL", False) and (macd_total <= 0 or signal_total <= 0):
        return True, f"macd_signal_zero macd={macd_total} signal={signal_total}"
    return False, "current_sufficient"


def _restore_summary_db_seed_sync(db_path: Any = None, *, force: bool = False) -> dict[str, Any]:
    global _RESTORED
    if _main_seed_restore_disabled(force=force):
        _RESTORED = True
        logger.warning(
            "[SUMMARY DB SEED RESTORE] skipped in main.py to avoid DB lock. main_database.py handles seed restore. set SUMMARY_DB_SEED_RESTORE_RUN_IN_MAIN=1 or force=True to override."
        )
        return {"ok": True, "skipped": True, "reason": "skip_in_main_process"}
    if not force and _RESTORED:
        logger.info("[SUMMARY DB SEED RESTORE] already restored -> skip")
        return {"ok": True, "skipped": True, "reason": "already_restored"}
    if not _env_bool("SUMMARY_DB_SEED_RESTORE_ENABLED", True):
        logger.warning("[SUMMARY DB SEED RESTORE] disabled by env SUMMARY_DB_SEED_RESTORE_ENABLED")
        return {"ok": False, "skipped": True, "reason": "disabled"}
    path = _resolve_db_path(db_path)
    if path is None:
        logger.warning("[SUMMARY DB SEED RESTORE] db path unresolved")
        return {"ok": False, "reason": "db_path_unresolved"}
    current_requested_path = path
    target_date = _target_date_for_current_path(current_requested_path)
    if not path.exists():
        logger.warning("[SUMMARY DB SEED RESTORE] db missing path=%s", path)
        prev_path = _resolve_previous_seed_db_path(path)
        if prev_path is None:
            return {"ok": False, "reason": "db_missing", "path": str(path)}
        if _env_bool("SUMMARY_DB_SEED_RESTORE_ALLOW_PREV_AS_PUSH_HISTORY", False):
            logger.warning("[SUMMARY DB SEED RESTORE] use previous summary DB because current missing current=%s previous=%s", path, prev_path)
            path = prev_path
            target_date = _parse_summary_date(prev_path)
        else:
            logger.warning("[SUMMARY DB SEED RESTORE] current missing but previous push-history restore disabled current=%s previous=%s", path, prev_path)
            return {"ok": False, "reason": "db_missing_previous_history_disabled", "path": str(path), "previous": str(prev_path)}
    per_symbol_rows = max(1, _env_int("SUMMARY_DB_SEED_RESTORE_BARS_PER_SYMBOL", 75))
    max_rows_default = max(1000, per_symbol_rows * 6000)
    max_rows = max(1000, _env_int("SUMMARY_DB_SEED_RESTORE_MAX_ROWS_PER_TF", max_rows_default))
    try:
        result = _restore_from_path(path, per_symbol_rows=per_symbol_rows, max_rows=max_rows, target_date=target_date)
        result["seed_source"] = "current_summary_db"
        needs_prev, reason = _result_needs_previous_seed(result, per_symbol_rows=per_symbol_rows)
        if needs_prev:
            prev_path = _resolve_previous_seed_db_path(path)
            if prev_path is not None and str(prev_path) != str(path):
                if _env_bool("SUMMARY_DB_SEED_RESTORE_ALLOW_PREV_AS_PUSH_HISTORY", False):
                    logger.warning("[SUMMARY DB SEED RESTORE] current summary DB insufficient -> restore previous seed current=%s previous=%s reason=%s per_symbol_rows=%s allow_prev_as_push_history=True", path, prev_path, reason, per_symbol_rows)
                    prev_target_date = _parse_summary_date(prev_path)
                    prev_result = _restore_from_path(prev_path, per_symbol_rows=per_symbol_rows, max_rows=max_rows, target_date=prev_target_date)
                    prev_result["seed_source"] = "previous_summary_db_insufficient_current"
                    prev_result["current_insufficient_path"] = str(path)
                    prev_result["current_insufficient_reason"] = reason
                    result = prev_result
                else:
                    logger.warning("[SUMMARY DB SEED RESTORE] current summary DB insufficient but previous seed is NOT injected into push history current=%s previous=%s reason=%s. Keep current-day seed only.", path, prev_path, reason)
                    result["seed_source"] = "current_summary_db_insufficient_prev_not_injected"
                    result["current_insufficient_reason"] = reason
                    result["previous_candidate_path"] = str(prev_path)
            else:
                result["seed_source"] = "current_summary_db_insufficient_no_previous"
                result["current_insufficient_reason"] = reason
        else:
            result["current_sufficient_reason"] = reason
        _RESTORED = True
        logger.warning("[SUMMARY DB SEED RESTORE] done path=%s seed_source=%s total_rows=%s reason=%s target_date=%s", result.get("path"), result.get("seed_source"), _total_restored_rows(result), result.get("current_insufficient_reason") or result.get("current_sufficient_reason"), result.get("target_date"))
        return result
    except Exception as e:
        logger.exception("[SUMMARY DB SEED RESTORE] failed path=%s", path)
        return {"ok": False, "reason": "exception", "error": str(e), "path": str(path)}


def _async_worker(seq: int, db_path: Any, kwargs: dict[str, Any]) -> None:
    global _ASYNC_THREAD, _ASYNC_STARTED_AT
    try:
        setattr(_ASYNC_ACTIVE, "running", True)
        logger.warning("[SUMMARY DB SEED RESTORE ASYNC] worker start seq=%s db_path=%s kwargs=%s", seq, db_path, kwargs)
        res = _restore_summary_db_seed_sync(db_path, **kwargs)
        logger.warning("[SUMMARY DB SEED RESTORE ASYNC] worker done seq=%s elapsed=%.3fs rows=%s ok=%s reason=%s", seq, _async_age(), _total_restored_rows(res) if isinstance(res, dict) else 0, (res or {}).get("ok") if isinstance(res, dict) else None, (res or {}).get("reason") if isinstance(res, dict) else None)
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE ASYNC] worker failed seq=%s", seq)
    finally:
        setattr(_ASYNC_ACTIVE, "running", False)
        with _ASYNC_LOCK:
            try:
                cur_name = getattr(_ASYNC_THREAD, "name", "") if _ASYNC_THREAD is not None else ""
                if cur_name == f"summary-db-seed-restore-async-{seq}":
                    _ASYNC_THREAD = None
                    _ASYNC_STARTED_AT = None
            except Exception:
                pass


def restore_summary_db_seed(db_path: Any = None, *, force: bool = False) -> dict[str, Any]:
    global _ASYNC_THREAD, _ASYNC_STARTED_AT, _ASYNC_SEQ
    if _main_seed_restore_disabled(force=force):
        return _restore_summary_db_seed_sync(db_path, force=force)
    if force or bool(getattr(_ASYNC_ACTIVE, "running", False)):
        return _restore_summary_db_seed_sync(db_path, force=force)
    if _env_bool("SUMMARY_DB_SEED_RESTORE_ASYNC_IN_MAIN", True) and _is_main_py_context():
        with _ASYNC_LOCK:
            if _async_alive():
                logger.warning("[SUMMARY DB SEED RESTORE ASYNC] already running -> return immediately age=%.3fs thread=%s", _async_age(), getattr(_ASYNC_THREAD, "name", ""))
                return {"ok": True, "skipped": True, "async_running": True, "reason": "async_worker_already_running"}
            _ASYNC_SEQ += 1
            seq = _ASYNC_SEQ
            _ASYNC_STARTED_AT = time.time()
            _ASYNC_THREAD = threading.Thread(
                target=_async_worker,
                args=(seq, db_path, {"force": force}),
                name=f"summary-db-seed-restore-async-{seq}",
                daemon=True,
            )
            _ASYNC_THREAD.start()
            logger.warning("[SUMMARY DB SEED RESTORE ASYNC] started worker seq=%s -> return immediately main_context=True", seq)
            return {"ok": True, "skipped": True, "async_started": True, "seq": seq, "reason": "async_in_main"}
    return _restore_summary_db_seed_sync(db_path, force=force)


__all__ = ["restore_summary_db_seed"]
