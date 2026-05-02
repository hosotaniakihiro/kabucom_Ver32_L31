# ============================================================
# File   : core/startup/ranking_bootstrap.py
# Ver    : PRODUCTION-STABLE-RANKING-BOOTSTRAP-DB-RESTORE-V1
# ------------------------------------------------------------
# ✔ 起動時に ranking DB から履歴復元
# ✔ ranking_snapshot_1min 読込
# ✔ 当日 + 前営業日対応
# ✔ global_data cache 復元
# ✔ ranking_summary_engine.rebuild_ranking_summaries 利用
# ✔ SQLite defensive coding
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

from utils.business_day_utils import get_previous_business_day, is_today_business_day

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()

from trading.ranking.ranking_summary_engine import (
    rebuild_ranking_summaries,
    set_ranking_summary_initialized,
)
from trading.ranking.summary.cache_store import (
    set_ranking_summary,
    set_latest_ranking_summary,
)

logger = logging.getLogger(__name__)


# ============================================================
# path helpers
# ============================================================

def _today() -> dt.date:
    return dt.date.today()


def _resolve_ranking_db_path(target_date: dt.date) -> str:
    ymd = target_date.strftime("%Y%m%d")
    return rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking{ymd}.db"


def _connect_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)

    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass

    try:
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass

    try:
        conn.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass

    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    try:
        cur = conn.execute(f"PRAGMA table_info({table_name})")
        return [str(r[1]) for r in cur.fetchall() if len(r) > 1]
    except Exception:
        return []


def _pick_column(columns: list[str], candidates: list[str]) -> Optional[str]:
    lower_map = {str(c).lower(): str(c) for c in columns}
    for c in candidates:
        x = lower_map.get(str(c).lower())
        if x:
            return x
    return None


# ============================================================
# dataframe normalize
# ============================================================

def _normalize_symbol(v) -> str:
    try:
        s = str(v).strip()
        if s.lower() in {"", "nan", "none", "nat", "null"}:
            return ""
        if "." in s:
            s = s.split(".", 1)[0].strip()
        return s
    except Exception:
        return ""


def _normalize_snapshot_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [
            "_".join([str(x) for x in tup if str(x) != ""]).strip("_")
            for tup in out.columns
        ]

    out.columns = [str(c).strip() for c in out.columns]
    if out.columns.duplicated().any():
        out = out.loc[:, ~out.columns.duplicated()].copy()

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].map(_normalize_symbol)
        out = out[out["symbol"] != ""].copy()

    if "snapshot_time" in out.columns:
        out["snapshot_time"] = pd.to_datetime(out["snapshot_time"], errors="coerce")
    elif "datetime" in out.columns:
        out["snapshot_time"] = pd.to_datetime(out["datetime"], errors="coerce")

    if "rank_position" in out.columns:
        out["rank_position"] = pd.to_numeric(out["rank_position"], errors="coerce")

    if "current_price" in out.columns:
        out["current_price"] = pd.to_numeric(out["current_price"], errors="coerce")

    if "value" in out.columns:
        out["value"] = pd.to_numeric(out["value"], errors="coerce")

    keep = [c for c in [
        "symbol",
        "symbolname",
        "snapshot_time",
        "rank_type",
        "rank_type_id",
        "market",
        "rank_position",
        "value",
        "current_price",
        "change_percentage",
        "change_ratio",
        "trading_volume",
        "trading_value",
        "turnover",
        "tick_count",
        "source",
    ] if c in out.columns]

    if keep:
        out = out[keep].copy()

    dedup_cols = [c for c in ["symbol", "snapshot_time", "rank_type", "market"] if c in out.columns]
    if dedup_cols:
        out = out.drop_duplicates(subset=dedup_cols, keep="last")

    if {"symbol", "snapshot_time"}.issubset(out.columns):
        out = out.sort_values(["symbol", "snapshot_time"], kind="stable")

    return out.reset_index(drop=True)


# ============================================================
# DB load
# ============================================================

def _load_snapshot_history_one_day(
    target_date: dt.date,
    *,
    start_dt: Optional[dt.datetime] = None,
    end_dt: Optional[dt.datetime] = None,
    limit_rows: Optional[int] = None,
) -> pd.DataFrame:
    db_path = _resolve_ranking_db_path(target_date)
    if not Path(db_path).exists():
        logger.info("[ranking_bootstrap] ranking db not found date=%s path=%s", target_date, db_path)
        return pd.DataFrame()

    try:
        with _connect_sqlite(db_path) as conn:
            if not _table_exists(conn, "ranking_snapshot_1min"):
                logger.info("[ranking_bootstrap] table missing date=%s table=ranking_snapshot_1min", target_date)
                return pd.DataFrame()

            cols = _get_table_columns(conn, "ranking_snapshot_1min")
            symbol_col = _pick_column(cols, ["symbol", "code"])
            dt_col = _pick_column(cols, ["snapshot_time", "datetime", "timestamp", "dt"])
            if not symbol_col or not dt_col:
                logger.warning(
                    "[ranking_bootstrap] required columns missing date=%s symbol_col=%s dt_col=%s cols=%s",
                    target_date, symbol_col, dt_col, cols
                )
                return pd.DataFrame()

            sql = f"""
                SELECT *
                FROM ranking_snapshot_1min
                WHERE {symbol_col} IS NOT NULL
                  AND TRIM({symbol_col}) <> ''
            """
            params: list[object] = []

            if start_dt is not None:
                sql += f" AND {dt_col} >= ?"
                params.append(start_dt.strftime("%Y-%m-%d %H:%M:%S"))

            if end_dt is not None:
                sql += f" AND {dt_col} <= ?"
                params.append(end_dt.strftime("%Y-%m-%d %H:%M:%S"))

            sql += f" ORDER BY {dt_col} ASC"

            if limit_rows and int(limit_rows) > 0:
                sql += f" LIMIT {int(limit_rows)}"

            df = pd.read_sql(sql, conn, params=params)
            df = _normalize_snapshot_df(df)

            logger.info(
                "[ranking_bootstrap] loaded date=%s rows=%s latest=%s path=%s",
                target_date,
                len(df),
                "-" if df.empty or "snapshot_time" not in df.columns else df["snapshot_time"].max(),
                db_path,
            )
            return df

    except Exception:
        logger.exception("[ranking_bootstrap] load failed date=%s path=%s", target_date, db_path)
        return pd.DataFrame()


def load_recent_ranking_snapshot_history(
    *,
    include_previous_business_day: bool = True,
    intraday_only: bool = True,
) -> pd.DataFrame:
    today = _today()
    dates: list[dt.date] = []

    if include_previous_business_day:
        try:
            prev_bd = get_previous_business_day(today)
        except Exception:
            prev_bd = today - dt.timedelta(days=1)
        dates.append(prev_bd)

    dates.append(today)

    now_dt = dt.datetime.now()

    dfs: list[pd.DataFrame] = []

    for d in dates:
        start_dt = None
        end_dt = None

        if intraday_only:
            start_dt = dt.datetime.combine(d, dt.time(9, 0, 0))
            if d == today and is_today_business_day():
                end_dt = now_dt
            else:
                end_dt = dt.datetime.combine(d, dt.time(15, 30, 0))

        df = _load_snapshot_history_one_day(
            d,
            start_dt=start_dt,
            end_dt=end_dt,
            limit_rows=None,
        )
        if not df.empty:
            dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    out = pd.concat(dfs, ignore_index=True)
    out = _normalize_snapshot_df(out)

    logger.info(
        "[ranking_bootstrap] concatenated history rows=%s symbols=%s latest=%s",
        len(out),
        0 if out.empty or "symbol" not in out.columns else out["symbol"].nunique(),
        "-" if out.empty or "snapshot_time" not in out.columns else out["snapshot_time"].max(),
    )
    return out.reset_index(drop=True)


# ============================================================
# cache restore
# ============================================================

def _save_history_to_global(df_1m: pd.DataFrame) -> None:
    try:
        global_data.ranking_snapshot_1min = df_1m.copy()
        global_data.ranking_snapshot = df_1m.copy()
        global_data.latest_ranking_df = df_1m.copy()

        latest_ts = None
        if not df_1m.empty and "snapshot_time" in df_1m.columns:
            s = pd.to_datetime(df_1m["snapshot_time"], errors="coerce").dropna()
            if not s.empty:
                latest_ts = s.max()

        global_data.ranking_snapshot_last_time = latest_ts
        global_data.ranking_last_updated_at = dt.datetime.now()

    except Exception:
        logger.exception("[ranking_bootstrap] save history to global failed")


def restore_ranking_summaries_from_db(
    *,
    include_previous_business_day: bool = True,
    announce: bool = False,
    use_discord: bool = False,
) -> dict:
    try:
        df_1m = load_recent_ranking_snapshot_history(
            include_previous_business_day=include_previous_business_day,
            intraday_only=True,
        )

        if df_1m.empty:
            logger.info("[ranking_bootstrap] restore skipped: empty ranking history")
            return {
                "ok": False,
                "loaded_rows": 0,
                "rebuilt_1m": 0,
                "rebuilt_3m": 0,
                "rebuilt_5m": 0,
            }

        _save_history_to_global(df_1m)

        rebuilt = rebuild_ranking_summaries(
            df_1m,
            announce_1m=announce,
            announce_3m=announce,
            announce_5m=announce,
            use_discord=use_discord,
        )

        df1 = rebuilt.get(1) if isinstance(rebuilt, dict) else pd.DataFrame()
        df3 = rebuilt.get(3) if isinstance(rebuilt, dict) else pd.DataFrame()
        df5 = rebuilt.get(5) if isinstance(rebuilt, dict) else pd.DataFrame()

        try:
            if isinstance(df1, pd.DataFrame):
                set_ranking_summary(1, df1)
                set_latest_ranking_summary(1, df1)
            if isinstance(df3, pd.DataFrame):
                set_ranking_summary(3, df3)
                set_latest_ranking_summary(3, df3)
            if isinstance(df5, pd.DataFrame):
                set_ranking_summary(5, df5)
                set_latest_ranking_summary(5, df5)

            set_ranking_summary_initialized(True)
        except Exception:
            logger.exception("[ranking_bootstrap] cache_store write failed")

        result = {
            "ok": True,
            "loaded_rows": int(len(df_1m)),
            "loaded_symbols": int(df_1m["symbol"].nunique()) if "symbol" in df_1m.columns else 0,
            "rebuilt_1m": 0 if not isinstance(df1, pd.DataFrame) else int(len(df1)),
            "rebuilt_3m": 0 if not isinstance(df3, pd.DataFrame) else int(len(df3)),
            "rebuilt_5m": 0 if not isinstance(df5, pd.DataFrame) else int(len(df5)),
            "latest_dt": "-" if "snapshot_time" not in df_1m.columns or df_1m.empty else str(df_1m["snapshot_time"].max()),
        }

        logger.info("[ranking_bootstrap] restore done result=%s", result)
        return result

    except Exception:
        logger.exception("[ranking_bootstrap] restore failed")
        return {
            "ok": False,
            "loaded_rows": 0,
            "rebuilt_1m": 0,
            "rebuilt_3m": 0,
            "rebuilt_5m": 0,
        }


__all__ = [
    "load_recent_ranking_snapshot_history",
    "restore_ranking_summaries_from_db",
]