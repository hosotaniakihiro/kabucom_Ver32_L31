# ============================================================
# File   : core/startup/summary_runtime_pkg/db_seed_multiday_sqlite.py
# Version: REV1.1-SUMMARY-RUNTIME-DB-SEED-MULTIDAY-SQLITE
#          -DB-FILE-DATE-FILTER
#          -STRICT-PER-DB-TARGET-DATE
#          -ROWID-FALLBACK
#          -DATETIME-FALLBACK
# ------------------------------------------------------------
# 【概要】
#   起動時 summary DB seed 用の multi-day SQLite direct loader
#
# 【主な機能】
#   ✔ 当日DB + 前営業日DB を直接読む
#   ✔ symbol ごとの tail 取得
#   ✔ rowid が無い table でも fallback
#   ✔ 日別 summaryYYYYMMDD.db 構成に対応
#
# 【REV1.1 修正】
#   ✔ DBファイル名 summaryYYYYMMDD.db から対象日を抽出
#   ✔ summary20260420.db を読むときは 2026-04-20 の行だけ読む
#   ✔ summary20260421.db を読むときは 2026-04-21 の行だけ読む
#   ✔ 前営業日DBなのに latest_dt=当日 になる混入を防止
#   ✔ date列が無い場合は datetime/date(time_range) fallback で date filter
#   ✔ DBごとの date breakdown ログを追加
#
# 【目的】
#   target_dates に前営業日を含めるだけでは、
#   日別DB内の不正混入行や別日行を除外できない。
#   本版では DBファイルごとに target date を固定し、
#   multi-day direct seed の品質を安定化する。
# ============================================================

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from .dataframe_utils import normalize_datetime_for_tf, dedupe_symbol_datetime, tail_per_symbol
from .db_seed_anchor import derive_summary_db_paths_for_dates
from .db_seed_diagnostics import log_history_quality, safe_symbols_count
from .db_seed_policy import get_summary_table, latest_dt

logger = logging.getLogger(__name__)


# ============================================================
# normalize
# ============================================================

def _normalize_seed_df(
    df: pd.DataFrame,
    tf: int,
    *,
    bars: Optional[int] = None,
) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    try:
        df = normalize_datetime_for_tf(df, tf)
    except Exception:
        logger.debug(
            "[summary_runtime] multi-day normalize_datetime_for_tf failed tf=%s",
            tf,
            exc_info=True,
        )

    if "symbol" in df.columns:
        try:
            df["symbol"] = df["symbol"].astype(str).str.strip()
            df = df[df["symbol"].ne("")].copy()
        except Exception:
            logger.debug(
                "[summary_runtime] multi-day symbol normalize failed tf=%s",
                tf,
                exc_info=True,
            )

    if "datetime" in df.columns:
        try:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            try:
                if getattr(df["datetime"].dt, "tz", None) is not None:
                    df["datetime"] = df["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            df = df.dropna(subset=["datetime"]).copy()
        except Exception:
            logger.debug(
                "[summary_runtime] multi-day datetime normalize/drop failed tf=%s",
                tf,
                exc_info=True,
            )

    try:
        df = dedupe_symbol_datetime(df)
    except Exception:
        logger.debug(
            "[summary_runtime] multi-day dedupe failed tf=%s",
            tf,
            exc_info=True,
        )

    if bars is not None:
        try:
            df = tail_per_symbol(df, int(bars))
        except Exception:
            logger.debug(
                "[summary_runtime] multi-day tail failed tf=%s bars=%s",
                tf,
                bars,
                exc_info=True,
            )

    return df


# ============================================================
# sqlite helpers
# ============================================================

def sqlite_table_exists(db_path: str, table: str) -> bool:
    try:
        with sqlite3.connect(db_path, timeout=5.0) as con:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            return row is not None
    except Exception:
        logger.debug(
            "[summary_runtime] sqlite table exists failed db=%s table=%s",
            db_path,
            table,
            exc_info=True,
        )
        return False


def sqlite_table_columns(db_path: str, table: str) -> list[str]:
    try:
        with sqlite3.connect(db_path, timeout=5.0) as con:
            rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
            return [str(r[1]) for r in rows if len(r) > 1 and r[1]]
    except Exception:
        logger.debug(
            "[summary_runtime] sqlite table columns failed db=%s table=%s",
            db_path,
            table,
            exc_info=True,
        )
        return []


def build_direct_dt_expr(cols: list[str]) -> str:
    """
    datetime 判定式を返す。

    1min は datetime があることが多い。
    3min / 5min は date + time_range unique の場合がある。
    """
    cset = set(cols)

    if "datetime" in cset:
        return "datetime"

    if "date" in cset and "time" in cset:
        return "datetime(date || ' ' || time)"

    if "date" in cset and "start_time" in cset:
        return "datetime(date || ' ' || start_time)"

    if "date" in cset and "time_range" in cset:
        return "datetime(date || ' ' || substr(time_range, 1, 5))"

    return "rowid"


# ============================================================
# date filter helpers
# ============================================================

def _extract_yyyymmdd_from_summary_db_path(db_path: str) -> Optional[str]:
    """
    summaryYYYYMMDD.db から YYYYMMDD を抽出する。
    """
    try:
        name = Path(str(db_path)).name
        m = re.search(r"summary(\d{8})\.db$", name, flags=re.IGNORECASE)
        if not m:
            return None
        return m.group(1)
    except Exception:
        return None


def _yyyymmdd_to_iso(yyyymmdd: Optional[str]) -> Optional[str]:
    if not yyyymmdd:
        return None

    try:
        ts = pd.to_datetime(str(yyyymmdd), format="%Y%m%d", errors="coerce")
        if pd.isna(ts):
            return None
        return ts.strftime("%Y-%m-%d")
    except Exception:
        return None


def _db_target_date_iso(db_path: str) -> Optional[str]:
    """
    DBファイル名から、このDBで読むべき対象日 YYYY-MM-DD を返す。
    """
    return _yyyymmdd_to_iso(_extract_yyyymmdd_from_summary_db_path(db_path))


def _build_target_date_condition(
    *,
    cols: list[str],
    dt_expr: str,
    target_date: Optional[str],
    params: dict[str, Any],
) -> Optional[str]:
    """
    DBファイルごとの target date filter SQL を返す。

    優先:
      1. date列があれば date = :target_date
      2. datetime列があれば date(datetime) = :target_date
      3. date + time/time_range/start_time fallback の dt_expr に対して date(dt_expr)
    """
    if not target_date:
        return None

    params["target_date"] = str(target_date)

    cset = set(cols)

    if "date" in cset:
        return "date = :target_date"

    if "datetime" in cset:
        return "date(datetime) = :target_date"

    if dt_expr and dt_expr != "rowid":
        return f"date({dt_expr}) = :target_date"

    return None


def _log_date_breakdown(df: pd.DataFrame, *, tf: int, db_path: str, target_date: Optional[str]) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.info(
                "[summary_runtime] direct multi-day seed date breakdown tf=%s db=%s target_date=%s rows=0",
                tf,
                db_path,
                target_date,
            )
            return

        tmp = df.copy()

        if "datetime" in tmp.columns:
            tmp["__date"] = pd.to_datetime(tmp["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        elif "date" in tmp.columns:
            tmp["__date"] = tmp["date"].astype(str).str[:10]
        else:
            logger.info(
                "[summary_runtime] direct multi-day seed date breakdown tf=%s db=%s target_date=%s rows=%d date_col_missing",
                tf,
                db_path,
                target_date,
                len(tmp),
            )
            return

        vc = tmp["__date"].fillna("NaT").value_counts().sort_index()
        summary = {str(k): int(v) for k, v in vc.items()}

        logger.info(
            "[summary_runtime] direct multi-day seed date breakdown tf=%s db=%s target_date=%s by_date=%s",
            tf,
            db_path,
            target_date,
            summary,
        )

    except Exception:
        logger.debug(
            "[summary_runtime] direct multi-day seed date breakdown failed tf=%s db=%s",
            tf,
            db_path,
            exc_info=True,
        )


# ============================================================
# one DB loader
# ============================================================

def read_tail_from_one_summary_db(
    *,
    db_path: str,
    tf: int,
    bars_per_symbol: int,
    max_allowed_dt: Optional[pd.Timestamp] = None,
    target_date: Optional[str] = None,
) -> pd.DataFrame:
    """
    1つの summaryYYYYMMDD.db から symbol ごとの tail を読む。

    REV1.1:
      target_date を指定すると、その日付の行だけ読む。
      指定が無い場合は db_path の summaryYYYYMMDD.db から自動推定する。
    """
    table = get_summary_table(int(tf))
    if not table:
        return pd.DataFrame()

    if not sqlite_table_exists(db_path, table):
        logger.warning(
            "[summary_runtime] direct multi-day seed table not found db=%s table=%s",
            db_path,
            table,
        )
        return pd.DataFrame()

    cols = sqlite_table_columns(db_path, table)
    if not cols:
        return pd.DataFrame()

    dt_expr = build_direct_dt_expr(cols)

    target_date = target_date or _db_target_date_iso(db_path)

    wheres = [
        "symbol IS NOT NULL",
        "TRIM(symbol) <> ''",
    ]
    params: dict[str, Any] = {
        "bars_per_symbol": int(bars_per_symbol),
    }

    if "datetime" in cols:
        wheres.append("datetime IS NOT NULL")
    elif "date" in cols:
        wheres.append("date IS NOT NULL")

    date_condition = _build_target_date_condition(
        cols=cols,
        dt_expr=dt_expr,
        target_date=target_date,
        params=params,
    )
    if date_condition:
        wheres.append(date_condition)
    else:
        logger.warning(
            "[summary_runtime] direct multi-day seed target_date filter unavailable tf=%s db=%s table=%s target_date=%s cols=%s dt_expr=%s",
            tf,
            db_path,
            table,
            target_date,
            cols,
            dt_expr,
        )

    if max_allowed_dt is not None and pd.notna(max_allowed_dt):
        # target_date filter があるため、前営業日DBにも max_allowed_dt を掛けても問題ない。
        # ただし dt_expr が rowid の場合は datetime 上限を掛けられない。
        if dt_expr != "rowid":
            wheres.append(f"{dt_expr} <= :max_allowed_dt")
            params["max_allowed_dt"] = pd.to_datetime(max_allowed_dt).strftime("%Y-%m-%d %H:%M:%S")

    sql_with_rowid = f"""
    WITH base AS (
        SELECT
            rowid AS __rid,
            *
        FROM "{table}"
        WHERE {" AND ".join(wheres)}
    ),
    ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY symbol
                ORDER BY {dt_expr} DESC, __rid DESC
            ) AS rn
        FROM base
    )
    SELECT *
    FROM ranked
    WHERE rn <= :bars_per_symbol
    ORDER BY symbol ASC, {dt_expr} ASC
    """

    sql_no_rowid = f"""
    WITH base AS (
        SELECT *
        FROM "{table}"
        WHERE {" AND ".join(wheres)}
    ),
    ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY symbol
                ORDER BY {dt_expr} DESC
            ) AS rn
        FROM base
    )
    SELECT *
    FROM ranked
    WHERE rn <= :bars_per_symbol
    ORDER BY symbol ASC, {dt_expr} ASC
    """

    try:
        with sqlite3.connect(db_path, timeout=10.0) as con:
            try:
                df = pd.read_sql_query(sql_with_rowid, con, params=params)
            except Exception as e:
                msg = str(e)
                if "no such column: rowid" not in msg and "no such column: __rid" not in msg:
                    raise

                logger.warning(
                    "[summary_runtime] direct multi-day seed rowid unavailable -> retry without rowid tf=%s db=%s table=%s",
                    tf,
                    db_path,
                    table,
                )
                df = pd.read_sql_query(sql_no_rowid, con, params=params)

        if df is None or df.empty:
            logger.info(
                "[summary_runtime] direct multi-day seed one db empty tf=%s db=%s table=%s target_date=%s",
                tf,
                db_path,
                table,
                target_date,
            )
            return pd.DataFrame()

        df = df.drop(columns=["rn", "__rid"], errors="ignore")
        df["__seed_db_path"] = db_path
        df["__seed_target_date"] = target_date or ""

        df = _normalize_seed_df(df, int(tf), bars=None)

        _log_date_breakdown(df, tf=int(tf), db_path=db_path, target_date=target_date)

        logger.info(
            "[summary_runtime] direct multi-day seed one db loaded tf=%s db=%s target_date=%s rows=%d symbols=%d latest_dt=%s",
            tf,
            db_path,
            target_date,
            len(df),
            safe_symbols_count(df),
            latest_dt(df),
        )

        return df

    except Exception:
        logger.debug(
            "[summary_runtime] direct multi-day seed read failed tf=%s db=%s table=%s target_date=%s",
            tf,
            db_path,
            table,
            target_date,
            exc_info=True,
        )
        return pd.DataFrame()


# ============================================================
# multi DB loader
# ============================================================

def _dates_iso_set(dates: Optional[Iterable[Any]]) -> set[str]:
    out: set[str] = set()

    for d in dates or []:
        try:
            ts = pd.to_datetime(d, errors="coerce")
            if pd.notna(ts):
                out.add(ts.strftime("%Y-%m-%d"))
        except Exception:
            continue

    return out


def load_summary_seed_by_multiday_sqlite_direct(
    tf: int,
    *,
    bars_per_symbol: int,
    dates: Optional[Iterable[Any]],
    max_allowed_dt: Optional[pd.Timestamp],
) -> pd.DataFrame:
    """
    当日DB + 前営業日DB を直接読んで、symbol ごとの tail を作る。

    REV1.1:
      各DBファイルに対して、ファイル名の日付に対応する行だけ読む。
    """
    db_paths = derive_summary_db_paths_for_dates(dates)
    if not db_paths:
        return pd.DataFrame()

    allowed_dates = _dates_iso_set(dates)

    frames: list[pd.DataFrame] = []

    for p in db_paths:
        target_date = _db_target_date_iso(p)

        if allowed_dates and target_date and target_date not in allowed_dates:
            logger.info(
                "[summary_runtime] direct multi-day seed skip db outside dates tf=%s db=%s target_date=%s allowed_dates=%s",
                tf,
                p,
                target_date,
                sorted(allowed_dates),
            )
            continue

        one = read_tail_from_one_summary_db(
            db_path=p,
            tf=int(tf),
            bars_per_symbol=int(bars_per_symbol),
            max_allowed_dt=max_allowed_dt,
            target_date=target_date,
        )
        if isinstance(one, pd.DataFrame) and not one.empty:
            frames.append(one)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True, sort=False)
    df = _normalize_seed_df(df, int(tf), bars=int(bars_per_symbol))

    if df.empty:
        return pd.DataFrame()

    log_history_quality(
        df,
        tf=int(tf),
        bars=int(bars_per_symbol),
        label="DB seed multi-day-direct",
    )

    _log_date_breakdown(
        df,
        tf=int(tf),
        db_path="MERGED_MULTI_DAY",
        target_date=",".join(sorted(allowed_dates)) if allowed_dates else None,
    )

    logger.info(
        "[summary_runtime] direct multi-day seed merged tf=%s rows=%d symbols=%d bars=%d latest_dt=%s",
        tf,
        len(df),
        safe_symbols_count(df),
        bars_per_symbol,
        latest_dt(df),
    )

    return df


__all__ = [
    "sqlite_table_exists",
    "sqlite_table_columns",
    "build_direct_dt_expr",
    "read_tail_from_one_summary_db",
    "load_summary_seed_by_multiday_sqlite_direct",
]