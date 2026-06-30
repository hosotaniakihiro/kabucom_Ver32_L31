# -*- coding: utf-8 -*-
"""
Runtime fixes for startup PUSH/summary/active-symbol issues.

REV3:
  - 2026-06-30 09:05 logs showed PUSH DB was live, but summary fallback still used
    stale summary_recovery_push_1m rows from 08:47-08:50 with volume=0.
  - Add a fresh fallback source built directly from pushYYYYMMDD.db / stream_data_raw.
  - Prefer fresh raw PUSH DB rows before older summary_recovery rows.

REV2:
  1. ACTIVE SUMMARY PRICE FALLBACK SQL emitted "incomplete input" because the
     correlated MAX(datetime) subquery was missing its closing parenthesis.
  2. PUSH summary fallback could miss persisted rows saved with source="push".
  3. Premarket SBI rows often have no price column; keep unknown-price symbols by default.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "REV3-PUSH-RAW-DB-FRESH-FALLBACK"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path() -> str:
    base = os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_today()}.db"))


def _push_db_path() -> str:
    base = os.getenv(
        "PUSH_DB_DIR",
        os.getenv(
            "RAW_PUSH_DIR",
            r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\push",
        ),
    )
    return os.getenv("PUSH_DB_PATH", str(Path(base) / f"push{_today()}.db"))


def _qident(name: Any) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None
    except Exception:
        return False


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({_qident(table)})").fetchall()}
    except Exception:
        return set()


def _summary_price_fallback_enabled() -> bool:
    if not _env_bool("ACTIVE_SUMMARY_PRICE_FALLBACK_ENABLED", True):
        return False
    if _is_main_py_process() and not _env_bool("ACTIVE_SUMMARY_PRICE_FALLBACK_RUN_IN_MAIN", False):
        return False
    return True


def _patched_summary_price_fallback_map(symbols: Iterable[str]) -> Dict[str, Dict[str, float]]:
    import trading.ranking.active_symbols.liquidity as liq

    cleaned = [liq.normalize_symbol(s) for s in liq.dedupe_keep_order(symbols)]
    cleaned = [s for s in cleaned if s]
    if not cleaned:
        return {}

    if not _summary_price_fallback_enabled():
        logger.warning(
            "[ACTIVE SUMMARY PRICE FALLBACK] skipped symbols=%d reason=disabled_or_main_process run_in_main=%s patch=%s",
            len(cleaned),
            os.getenv("ACTIVE_SUMMARY_PRICE_FALLBACK_RUN_IN_MAIN"),
            VERSION,
        )
        return {}

    path = _summary_db_path()
    if not path or not Path(path).exists():
        logger.warning("[ACTIVE SUMMARY PRICE FALLBACK] db not found path=%s symbols=%d patch=%s", path, len(cleaned), VERSION)
        return {}

    timeout_sec = max(0.05, _env_float("ACTIVE_SUMMARY_PRICE_FALLBACK_TIMEOUT_SEC", 0.35))
    busy_ms = int(max(50.0, _env_float("ACTIVE_SUMMARY_PRICE_FALLBACK_BUSY_TIMEOUT_MS", 300.0)))
    t0 = time.monotonic()
    out: Dict[str, Dict[str, float]] = {}

    try:
        with sqlite3.connect(path, timeout=timeout_sec) as conn:
            conn.execute(f"PRAGMA busy_timeout={busy_ms};")
            for table in ("stock_summary_1min", "stock_summary_3min", "stock_summary_5min"):
                if len(out) >= len(cleaned):
                    break
                if not _table_exists(conn, table):
                    continue
                cols = _table_cols(conn, table)
                if "symbol" not in cols:
                    continue

                if "datetime" in cols:
                    dt_expr = _qident("datetime")
                    dt_expr_t2 = f"t2.{_qident('datetime')}"
                elif "date" in cols and "time" in cols:
                    dt_expr = f"({_qident('date')} || ' ' || {_qident('time')})"
                    dt_expr_t2 = f"(t2.{_qident('date')} || ' ' || t2.{_qident('time')})"
                else:
                    continue

                price_col = None
                for c in ("current_price", "price", "close", "close_price"):
                    if c in cols:
                        price_col = c
                        break
                if not price_col:
                    continue

                remain = [s for s in cleaned if s not in out]
                if not remain:
                    break
                placeholders = ",".join(["?"] * len(remain))
                table_q = _qident(table)
                symbol_q = _qident("symbol")
                sql = f"""
                    SELECT CAST({symbol_q} AS TEXT) AS symbol,
                           {_qident(price_col)} AS price,
                           {dt_expr} AS dtv
                    FROM {table_q}
                    WHERE CAST({symbol_q} AS TEXT) IN ({placeholders})
                      AND {dt_expr} = (
                          SELECT MAX({dt_expr_t2})
                          FROM {table_q} t2
                          WHERE CAST(t2.{symbol_q} AS TEXT) = CAST({table_q}.{symbol_q} AS TEXT)
                      )
                """
                try:
                    rows = conn.execute(sql, remain).fetchall()
                except Exception as e:
                    logger.warning("[ACTIVE SUMMARY PRICE FALLBACK] bulk select skipped table=%s err=%s patch=%s", table, e, VERSION, exc_info=False)
                    continue

                for row in rows or []:
                    sym = liq.normalize_symbol(row[0])
                    price = liq.to_float(row[1], 0.0)
                    if sym and price > 0 and sym not in out:
                        out[sym] = {
                            "current_price": price,
                            "price": price,
                            "close": price,
                            "summary_price_table": table,
                        }
        logger.warning(
            "[ACTIVE SUMMARY PRICE FALLBACK] loaded symbols=%d hit=%d missing=%d elapsed=%.3fs path=%s patch=%s",
            len(cleaned),
            len(out),
            max(0, len(cleaned) - len(out)),
            time.monotonic() - t0,
            path,
            VERSION,
        )
        return out
    except sqlite3.OperationalError as e:
        logger.warning("[ACTIVE SUMMARY PRICE FALLBACK] sqlite skipped path=%s symbols=%d err=%s patch=%s", path, len(cleaned), e, VERSION, exc_info=False)
        return {}
    except Exception:
        logger.exception("[ACTIVE SUMMARY PRICE FALLBACK] failed path=%s symbols=%d patch=%s", path, len(cleaned), VERSION)
        return {}


def _patched_allow_unknown_price(*, premarket_mode: bool) -> bool:
    try:
        if _env_bool("ACTIVE_FINAL_PRICE_GUARD_ALLOW_UNKNOWN_PRICE", False):
            return True
        if premarket_mode and _env_bool("ACTIVE_PREMARKET_ALLOW_NO_PRICE", True):
            return True
        return False
    except Exception:
        return bool(premarket_mode)


def _patch_active_price_sql() -> bool:
    try:
        import trading.ranking.active_symbols.liquidity as liq
        liq._summary_price_fallback_map = _patched_summary_price_fallback_map
        liq._allow_unknown_price = _patched_allow_unknown_price
        logger.warning("[ACTIVE SUMMARY PRICE FALLBACK PATCH] installed version=%s premarket_unknown_price_default=True", VERSION)
        return True
    except Exception:
        logger.exception("[ACTIVE SUMMARY PRICE FALLBACK PATCH] install failed version=%s", VERSION)
        return False


def _safe_to_num(s):
    return pd.to_numeric(s, errors="coerce")


def _load_recent_push_raw_summary(interval_i: int, *, now_i: dt.datetime) -> pd.DataFrame:
    if not _env_bool("PUSH_SUMMARY_RAW_DB_FALLBACK_ENABLED", True):
        return pd.DataFrame()
    path = _push_db_path()
    p = Path(path)
    if not p.exists():
        logger.warning("[PUSH RAW DB FALLBACK] db not found path=%s patch=%s", path, VERSION)
        return pd.DataFrame()

    lookback_min = int(max(2, _env_float("PUSH_SUMMARY_RAW_DB_FALLBACK_LOOKBACK_MIN", 10.0)))
    limit = int(max(100, _env_float("PUSH_SUMMARY_RAW_DB_FALLBACK_LIMIT", 50000.0)))
    timeout_sec = max(0.05, _env_float("PUSH_SUMMARY_RAW_DB_FALLBACK_TIMEOUT_SEC", 0.8))
    busy_ms = int(max(50, _env_float("PUSH_SUMMARY_RAW_DB_FALLBACK_BUSY_TIMEOUT_MS", 500.0)))
    since = now_i - dt.timedelta(minutes=lookback_min)

    try:
        with sqlite3.connect(str(p), timeout=timeout_sec) as conn:
            conn.execute(f"PRAGMA busy_timeout={busy_ms};")
            table = "stream_data_raw" if _table_exists(conn, "stream_data_raw") else "stream_data"
            cols = _table_cols(conn, table)
            if not {"symbol", "datetime", "price"}.issubset(cols):
                logger.warning("[PUSH RAW DB FALLBACK] required cols missing table=%s cols=%s path=%s", table, sorted(cols), path)
                return pd.DataFrame()
            wanted = [
                "symbol", "symbolname", "datetime", "date", "time", "price", "volume",
                "trading_value", "vwap", "opening_price", "high_price", "low_price",
            ]
            if "received_at" in cols:
                wanted.append("received_at")
            select_cols = [c for c in wanted if c in cols]
            date_filter = now_i.strftime("%Y-%m-%d")
            where_parts = []
            params: list[Any] = []
            if "date" in cols:
                where_parts.append("date = ?")
                params.append(date_filter)
            if "received_at" in cols:
                where_parts.append("received_at >= ?")
                params.append(since.isoformat())
            else:
                where_parts.append("datetime >= ?")
                params.append(since.isoformat())
            where = " AND ".join(where_parts) if where_parts else "1=1"
            sql = f"SELECT {','.join(_qident(c) for c in select_cols)} FROM {_qident(table)} WHERE {where} ORDER BY datetime DESC LIMIT ?"
            params.append(limit)
            df = pd.read_sql_query(sql, conn, params=params)
    except Exception:
        logger.debug("[PUSH RAW DB FALLBACK] load failed path=%s interval=%s", path, interval_i, exc_info=True)
        return pd.DataFrame()

    if df.empty:
        logger.warning("[PUSH RAW DB FALLBACK] empty path=%s interval=%s since=%s patch=%s", path, interval_i, since, VERSION)
        return df

    try:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime", "symbol"])
        df["price"] = _safe_to_num(df["price"])
        df = df.dropna(subset=["price"])
        df = df[df["price"] > 0].copy()
        if df.empty:
            return pd.DataFrame()
        df["symbol"] = df["symbol"].astype(str).str.strip()
        df = df[df["symbol"] != ""].copy()
        try:
            df["slot"] = df["datetime"].dt.floor(f"{int(interval_i)}min")
        except Exception:
            df["slot"] = df["datetime"]
        latest_slot = df["slot"].max()
        df = df[df["slot"] == latest_slot].copy()
        df = df.sort_values(["symbol", "datetime"])
        if "volume" in df.columns:
            df["volume"] = _safe_to_num(df["volume"]).fillna(0.0)
        else:
            df["volume"] = 0.0
        if "trading_value" in df.columns:
            df["trading_value"] = _safe_to_num(df["trading_value"]).fillna(0.0)
        else:
            df["trading_value"] = 0.0
        if "symbolname" not in df.columns:
            df["symbolname"] = ""

        grouped = df.groupby("symbol", sort=False)
        out = pd.DataFrame({
            "symbol": grouped["symbol"].last(),
            "symbolname": grouped["symbolname"].last(),
            "datetime": grouped["slot"].last(),
            "open": grouped["price"].first(),
            "high": grouped["price"].max(),
            "low": grouped["price"].min(),
            "close": grouped["price"].last(),
            "volume": grouped["volume"].max(),
            "trading_value": grouped["trading_value"].max(),
        }).reset_index(drop=True)
        out["price"] = out["close"]
        out["current_price"] = out["close"]
        out["open_price"] = out["open"]
        out["high_price"] = out["high"]
        out["low_price"] = out["low"]
        out["close_price"] = out["close"]
        out["interval"] = int(interval_i)
        out["source"] = "push_stream_raw_db"
        out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        out["time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
        out["start_time"] = out["time"]
        out["end_time"] = out["time"]
        logger.warning(
            "[PUSH RAW DB FALLBACK] loaded interval=%s rows=%s symbols=%s latest_dt=%s path=%s patch=%s",
            interval_i,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
            path,
            VERSION,
        )
        return out
    except Exception:
        logger.exception("[PUSH RAW DB FALLBACK] transform failed path=%s interval=%s patch=%s", path, interval_i, VERSION)
        return pd.DataFrame()


def _patch_push_summary_fallback() -> bool:
    try:
        import scheduler_jobs.summary.fallback_loader as fl

        orig_filter = getattr(fl, "filter_push_like_rows", None)
        orig_fallback = getattr(fl, "fallback_push_summary_df", None)

        def patched_filter_push_like_rows(df: pd.DataFrame) -> pd.DataFrame:
            x = fl.normalize_df(df)
            if x.empty or "source" not in x.columns:
                return x
            try:
                src = x["source"].astype(str)
                src_l = src.str.lower().str.strip()
                mask = (
                    src_l.isin({"push", "summary", "push_summary", "summary_push", "push_stream_raw_db"})
                    | src.str.contains("push_stream", case=False, na=False)
                    | src.str.contains("yahoo_pipeline", case=False, na=False)
                    | src.str.contains("incremental", case=False, na=False)
                    | src.str.contains("summary_recovery", case=False, na=False)
                    | src.str.contains("resample", case=False, na=False)
                )
                out = x.loc[mask].copy()
                logger.info(
                    "[summary.fallback_loader] push-like filter patched rows=%s -> %s source_dist=%s patch=%s",
                    len(x),
                    len(out),
                    {} if out.empty else out["source"].astype(str).value_counts().head(10).to_dict(),
                    VERSION,
                )
                return out.reset_index(drop=True)
            except Exception:
                logger.exception("[summary.fallback_loader] push-like filter patched failed")
                if callable(orig_filter):
                    return orig_filter(df)
                return x

        def _is_fresh_enough(df: pd.DataFrame, interval_i: int, now_i: dt.datetime) -> bool:
            try:
                ts = fl.extract_latest_timestamp(df)
                if ts is None:
                    return False
                return bool(fl.is_fresh_timestamp(ts, interval_i, for_ranking=False, now=now_i))
            except Exception:
                return False

        def patched_fallback_push_summary_df(interval: int, *, now=None) -> pd.DataFrame:
            interval_i = int(interval)
            now_i = (now or fl.now_naive()).replace(tzinfo=None, microsecond=0)

            raw_df = _load_recent_push_raw_summary(interval_i, now_i=now_i)
            if isinstance(raw_df, pd.DataFrame) and not raw_df.empty:
                raw_df = fl.normalize_df(raw_df)
                raw_df = fl._slot_aligned_latest_rows(raw_df, interval=interval_i, now=now_i)
                if not raw_df.empty and _is_fresh_enough(raw_df, interval_i, now_i):
                    logger.warning(
                        "[summary.fallback_loader] selected fresh push raw DB fallback interval=%s rows=%s symbols=%s latest_dt=%s patch=%s",
                        interval_i,
                        len(raw_df),
                        fl.symbols_count(raw_df),
                        fl.latest_dt_str(raw_df),
                        VERSION,
                    )
                    return raw_df.reset_index(drop=True)

            if callable(orig_fallback):
                try:
                    df0 = orig_fallback(interval_i, now=now_i)
                    if isinstance(df0, pd.DataFrame) and not df0.empty:
                        if _is_fresh_enough(df0, interval_i, now_i):
                            return df0
                        if not raw_df.empty:
                            return raw_df.reset_index(drop=True)
                except Exception:
                    logger.debug("[summary.fallback_loader] original push fallback failed interval=%s", interval_i, exc_info=True)

            candidates: list[tuple[str, pd.DataFrame]] = []
            if isinstance(raw_df, pd.DataFrame) and not raw_df.empty:
                candidates.append((f"db.push_raw[{interval_i}].patched", raw_df))
            for src in ("push", "SUMMARY", "summary", None):
                try:
                    df = fl.load_latest_summary_from_db(interval_i, source_filter=src, now=now_i)
                    df = patched_filter_push_like_rows(df)
                    df = fl._slot_aligned_latest_rows(df, interval=interval_i, now=now_i)
                    if not df.empty:
                        candidates.append((f"db.stock_summary_{interval_i}min[{src or '*'}].patched", df))
                except Exception:
                    logger.debug("[summary.fallback_loader] patched push fallback source failed interval=%s src=%s", interval_i, src, exc_info=True)

            df = fl.select_best_candidate(candidates, interval=interval_i, for_ranking=False, now=now_i)
            if not df.empty:
                logger.warning("[summary.fallback_loader] patched push fallback selected interval=%s rows=%s symbols=%s patch=%s", interval_i, len(df), fl.symbols_count(df), VERSION)
                return df

            logger.warning("[summary.fallback_loader] patched fallback push summary empty interval=%s now=%s patch=%s", interval_i, now_i, VERSION)
            return pd.DataFrame()

        fl.filter_push_like_rows = patched_filter_push_like_rows
        fl.fallback_push_summary_df = patched_fallback_push_summary_df

        try:
            import scheduler_jobs.summary.runner_core as rc
            rc.filter_push_like_rows = patched_filter_push_like_rows
            rc.fallback_push_summary_df = patched_fallback_push_summary_df
        except Exception:
            pass

        logger.warning("[PUSH SUMMARY FALLBACK PATCH] installed version=%s raw_db_fallback=True", VERSION)
        return True
    except Exception:
        logger.exception("[PUSH SUMMARY FALLBACK PATCH] install failed version=%s", VERSION)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    ok1 = _patch_active_price_sql()
    ok2 = _patch_push_summary_fallback()
    _INSTALLED = bool(ok1 or ok2)
    logger.warning("[PUSH SUMMARY/ACTIVE PRICE PATCH] installed=%s price=%s push_fallback=%s version=%s", _INSTALLED, ok1, ok2, VERSION)
    return _INSTALLED


__all__ = ["install", "VERSION"]
