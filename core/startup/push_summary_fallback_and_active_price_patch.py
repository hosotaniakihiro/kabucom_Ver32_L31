# -*- coding: utf-8 -*-
"""
Runtime fixes for 2026-06-15 startup logs.

Fixes:
  1. ACTIVE SUMMARY PRICE FALLBACK SQL emitted "incomplete input" because the
     correlated MAX(datetime) subquery was missing its closing parenthesis.
  2. PUSH summary fallback could miss persisted rows saved with source="push"
     because the fallback source list / push-like filter did not include the
     plain "push" source used by runner_core._save_summary_if_owner(...).

This module is intentionally narrow and safe to install from sitecustomize via
an already-loaded startup patch.
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
VERSION = "REV1-PUSH-SUMMARY-FALLBACK-ACTIVE-PRICE-SQL"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
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
                elif "date" in cols and "time" in cols:
                    dt_expr = f"({_qident('date')} || ' ' || {_qident('time')})"
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
                # The original SQL missed the final ')' after the correlated subquery.
                sql = f"""
                    SELECT CAST({symbol_q} AS TEXT) AS symbol,
                           {_qident(price_col)} AS price,
                           {dt_expr} AS dtv
                    FROM {table_q}
                    WHERE CAST({symbol_q} AS TEXT) IN ({placeholders})
                      AND {dt_expr} = (
                          SELECT MAX({dt_expr})
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


def _patch_active_price_sql() -> bool:
    try:
        import trading.ranking.active_symbols.liquidity as liq
        liq._summary_price_fallback_map = _patched_summary_price_fallback_map
        logger.warning("[ACTIVE SUMMARY PRICE FALLBACK PATCH] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[ACTIVE SUMMARY PRICE FALLBACK PATCH] install failed version=%s", VERSION)
        return False


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
                    src_l.isin({"push", "summary", "push_summary", "summary_push"})
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

        def patched_fallback_push_summary_df(interval: int, *, now=None) -> pd.DataFrame:
            if callable(orig_fallback):
                try:
                    df0 = orig_fallback(interval, now=now)
                    if isinstance(df0, pd.DataFrame) and not df0.empty:
                        return df0
                except Exception:
                    logger.debug("[summary.fallback_loader] original push fallback failed interval=%s", interval, exc_info=True)

            interval_i = int(interval)
            now_i = (now or fl.now_naive()).replace(tzinfo=None, microsecond=0)
            candidates: list[tuple[str, pd.DataFrame]] = []
            # Plain source='push' is used by runner_core DB saves.  Also try a broad
            # table read and let the patched push-like filter remove non-PUSH rows.
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

        # runner_core imports these functions by name, so patch already-loaded references too.
        try:
            import scheduler_jobs.summary.runner_core as rc
            rc.filter_push_like_rows = patched_filter_push_like_rows
            rc.fallback_push_summary_df = patched_fallback_push_summary_df
        except Exception:
            pass

        logger.warning("[PUSH SUMMARY FALLBACK PATCH] installed version=%s", VERSION)
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
