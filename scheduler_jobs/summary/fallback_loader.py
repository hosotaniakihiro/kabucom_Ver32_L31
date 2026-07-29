# -*- coding: utf-8 -*-
#====================================================================================================
# scheduler_jobs/summary/fallback_loader.py
#====================================================================================================
# ============================================================
# File   : scheduler_jobs/summary/fallback_loader.py
# Ver    : PRODUCTION-STABLE-SUMMARY-FALLBACK-LOADER-V2.4-INLINE-REV5-PUSH-FALLBACK
#          -NO-RECOVERY-FALLBACK-FOR-1M-PUSH
#          -EXPECTED-SLOT-AWARE
#          -NOW-PASSTHROUGH
#          -STALE-FALLBACK-SUPPRESSED
#          -MAIN-1M-MEMORY-FIRST-NO-NAS-DB
# ------------------------------------------------------------
# ✔ DB / cache fallback
# ✔ push-like source filter
# ✔ best candidate selection
# ✔ expected_slot 以下の最新 slot に整列
# ✔ now を scheduler 側から伝搬
# ✔ 古い fallback を安易に採用しない
# ✔ 1分 PUSH fallback では市場中の stale を抑制
# ✔ main.py の 1m PUSH fallback はメモリ/前回mergedを優先し、NAS DBを既定で読まない
#
# V2.4:
#   - 旧 core/startup/push_summary_fallback_and_active_price_patch.py (REV5) を
#     本文へインライン化。同日ガード (_same_day_push_rows/_latest_is_today)、
#     raw PUSH DB優先読み込み (_load_recent_push_raw_summary)、
#     main.py 1分足のraw/DB fallback禁止 (_main_1m_raw_db_fallback_blocked) を
#     filter_push_like_rows / fallback_push_summary_df 本体に統合。
#     旧来ロジックは _base_fallback_push_summary_df として維持し、
#     fallback_push_summary_df から内部的に呼び出す構成にした。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .display_prepare import normalize_df, extract_latest_timestamp, latest_dt_str, symbols_count
from .quality_guards import looks_uncomputed_push_df, looks_uncomputed_ranking_df
from .time_utils import (
    now_naive,
    today_date,
    is_future_timestamp,
    is_today_timestamp,
    is_fresh_timestamp,
    age_minutes,
    floor_to_interval,
    is_market_session,
)

logger = logging.getLogger(__name__)

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()


def safe_getattr(obj, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


# main_database.py がPUSH DB保存を担当する前提の raw/NAS DB fallback 既定値。
# 旧 core/startup/push_summary_fallback_and_active_price_patch.py の install() から移設。
os.environ.setdefault("SUMMARY_MAIN_DISABLE_RAW_DB_FALLBACK", "1")
os.environ.setdefault("PUSH_SUMMARY_RAW_DB_FALLBACK_RUN_IN_MAIN", "0")


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in (sys.argv or []))
    except Exception:
        return ""


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _is_main_entry_context() -> bool:
    try:
        argv = _argv_text()
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return "main.py" in argv or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return False


def _main_1m_skip_db_fallback() -> bool:
    # main.py では DB fallback が17〜25秒詰まるため既定でOFF。
    # 必要時だけ SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK=0 で戻せる。
    return _is_main_entry_context() and _env_bool("SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK", True)


def _primary_dt_col(df: pd.DataFrame) -> Optional[str]:
    for c in ("datetime", "end_time", "time", "start_time", "snapshot_time"):
        if c in df.columns:
            return c
    return None


def _slot_aligned_latest_rows(
    df: pd.DataFrame,
    *,
    interval: int,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """
    fallback 候補を expected_slot 以下の最新 slot に揃える。
    """
    x = normalize_df(df)
    if x.empty:
        return x

    col = _primary_dt_col(x)
    if not col:
        return x

    now = (now or now_naive()).replace(tzinfo=None, microsecond=0)

    try:
        s = pd.to_datetime(x[col], errors="coerce")
        try:
            s = s.dt.tz_localize(None)
        except Exception:
            pass

        x = x.loc[s.notna()].copy()
        if x.empty:
            return x

        s = pd.to_datetime(x[col], errors="coerce")
        try:
            s = s.dt.tz_localize(None)
        except Exception:
            pass

        x["_dt"] = s
        expected_slot = pd.Timestamp(floor_to_interval(now, interval))

        try:
            x["_slot"] = x["_dt"].dt.floor(f"{int(interval)}min")
        except Exception:
            x["_slot"] = x["_dt"]

        past = x.loc[x["_slot"] <= expected_slot].copy()
        if past.empty:
            logger.warning(
                "[summary.fallback_loader] slot align no past rows interval=%s expected_slot=%s latest_dt=%s",
                interval,
                expected_slot,
                latest_dt_str(x),
            )
            return pd.DataFrame()

        chosen_slot = past["_slot"].max()
        out = past.loc[past["_slot"] == chosen_slot].copy()

        logger.info(
            "[summary.fallback_loader] slot aligned interval=%s expected_slot=%s chosen_slot=%s rows=%s symbols=%s",
            interval,
            expected_slot,
            chosen_slot,
            len(out),
            symbols_count(out),
        )

        return out.drop(columns=["_dt", "_slot"], errors="ignore").reset_index(drop=True)

    except Exception:
        logger.exception("[summary.fallback_loader] slot align failed interval=%s", interval)
        return x.reset_index(drop=True)


def select_best_candidate(
    candidates: list[tuple[str, pd.DataFrame]],
    *,
    interval: int,
    for_ranking: bool = False,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    usable: list[tuple[str, pd.DataFrame, pd.Timestamp, bool, float]] = []
    now = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    expected_slot = floor_to_interval(now, interval)

    for name, src in candidates:
        df = normalize_df(src)
        if df.empty:
            logger.info("[summary.fallback_loader] candidate empty name=%s interval=%s", name, interval)
            continue

        df = _slot_aligned_latest_rows(df, interval=interval, now=now)
        if df.empty:
            logger.warning(
                "[summary.fallback_loader] candidate empty after slot align name=%s interval=%s expected_slot=%s",
                name,
                interval,
                expected_slot,
            )
            continue

        ts = extract_latest_timestamp(df)
        if ts is None:
            logger.warning("[summary.fallback_loader] candidate has no timestamp name=%s interval=%s", name, interval)
            continue

        if is_future_timestamp(ts, interval=interval, now=now):
            logger.warning("[summary.fallback_loader] fallback candidate skipped by future-ts name=%s interval=%s latest_dt=%s", name, interval, str(ts))
            continue

        if not is_today_timestamp(ts, now=now):
            logger.warning(
                "[summary.fallback_loader] fallback candidate skipped by non-today name=%s interval=%s latest_dt=%s today=%s",
                name,
                interval,
                str(ts),
                today_date(now=now),
            )
            continue

        if for_ranking:
            if looks_uncomputed_ranking_df(df):
                logger.warning("[summary.fallback_loader] fallback candidate skipped by uncomputed-ranking name=%s interval=%s latest_dt=%s", name, interval, str(ts))
                continue
        else:
            if looks_uncomputed_push_df(df):
                logger.warning("[summary.fallback_loader] fallback candidate skipped by uncomputed-push name=%s interval=%s latest_dt=%s", name, interval, str(ts))
                continue

        fresh = is_fresh_timestamp(ts, interval, for_ranking=for_ranking, now=now)
        age = age_minutes(ts, now=now)
        age = float(age) if age is not None else 999999.0

        logger.info(
            "[summary.fallback_loader] candidate usable-check name=%s interval=%s rows=%s symbols=%s latest_dt=%s age_min=%.2f fresh=%s expected_slot=%s",
            name,
            interval,
            len(df),
            symbols_count(df),
            str(ts),
            age,
            fresh,
            expected_slot,
        )

        usable.append((name, df, ts, fresh, age))

    if not usable:
        logger.warning("[summary.fallback_loader] no usable fallback candidates interval=%s for_ranking=%s expected_slot=%s", interval, for_ranking, expected_slot)
        return pd.DataFrame()

    fresh_only = [x for x in usable if x[3]]

    if interval <= 1 and not for_ranking and is_market_session(now):
        pool = fresh_only
        if not pool:
            logger.warning("[summary.fallback_loader] suppressed stale push fallback during market session interval=%s expected_slot=%s", interval, expected_slot)
            return pd.DataFrame()
    else:
        pool = fresh_only if fresh_only else usable

    pool.sort(key=lambda x: (x[2], len(x[1])), reverse=True)
    chosen_name, chosen_df, chosen_ts, chosen_fresh, chosen_age = pool[0]

    logger.info(
        "[summary.fallback_loader] fallback chosen name=%s interval=%s rows=%s symbols=%s latest_dt=%s age_min=%.2f fresh=%s expected_slot=%s",
        chosen_name,
        interval,
        len(chosen_df),
        symbols_count(chosen_df),
        str(chosen_ts),
        chosen_age,
        chosen_fresh,
        expected_slot,
    )
    return chosen_df.reset_index(drop=True)


def today_summary_db_path(*, now: Optional[dt.datetime] = None) -> Optional[Path]:
    candidates: list[str] = []

    for attr in ("summary_db_path", "current_summary_db_path", "resolved_summary_db_path"):
        try:
            v = safe_getattr(global_data, attr, None)
            if isinstance(v, (str, Path)) and str(v).strip():
                candidates.append(str(v))
        except Exception:
            pass

    base_date = today_date(now=now)
    candidates.append(rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary\summary{base_date:%Y%m%d}.db")

    for raw in candidates:
        try:
            p = Path(raw)
            if p.exists():
                return p
        except Exception:
            pass

    return None


def summary_table_name(interval: int) -> str:
    return f"stock_summary_{int(interval)}min"


def load_latest_summary_from_db(
    interval: int,
    *,
    limit_rows: int = 20000,
    source_filter: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    if int(interval) <= 1 and _main_1m_skip_db_fallback():
        logger.warning(
            "[summary.fallback_loader] DB fallback skipped in main.py interval=%s source=%s reason=SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK",
            interval,
            source_filter or "*",
        )
        return pd.DataFrame()

    db_path = today_summary_db_path(now=now)
    if db_path is None:
        return pd.DataFrame()

    table = summary_table_name(interval)
    sql = f"""
    SELECT *
    FROM {table}
    WHERE 1=1
    """
    params: list[Any] = []

    if source_filter:
        sql += " AND source = ? "
        params.append(source_filter)

    sql += """
    ORDER BY datetime DESC
    LIMIT ?
    """
    params.append(int(limit_rows))

    try:
        with sqlite3.connect(str(db_path), timeout=0.75) as conn:
            df = pd.read_sql_query(sql, conn, params=params)
    except Exception:
        logger.debug(
            "[summary.fallback_loader] db fallback load failed interval=%s table=%s source=%s path=%s",
            interval,
            table,
            source_filter,
            db_path,
            exc_info=True,
        )
        return pd.DataFrame()

    df = normalize_df(df)
    if df.empty:
        return df

    df = _slot_aligned_latest_rows(df, interval=interval, now=now)

    logger.info(
        "[summary.fallback_loader] db fallback loaded interval=%s source=%s rows=%s symbols=%s latest_dt=%s path=%s",
        interval,
        source_filter or "*",
        len(df),
        symbols_count(df),
        latest_dt_str(df),
        db_path,
    )
    return df


def _push_db_path() -> str:
    base = os.getenv(
        "PUSH_DB_DIR",
        os.getenv(
            "RAW_PUSH_DIR",
            r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\push",
        ),
    )
    today_s = dt.datetime.now().strftime("%Y%m%d")
    return os.getenv("PUSH_DB_PATH", str(Path(base) / f"push{today_s}.db"))


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


def _normalize_dt_series(s: Any) -> pd.Series:
    """Normalize datetime to tz-naive local/JST wall-clock without shifting naive rows."""
    def _one(v: Any) -> Any:
        try:
            x = pd.to_datetime(v, errors="coerce")
            if pd.isna(x):
                return pd.NaT
            if getattr(x, "tzinfo", None) is not None:
                try:
                    return x.tz_convert("Asia/Tokyo").tz_localize(None)
                except Exception:
                    try:
                        return x.tz_localize(None)
                    except Exception:
                        return pd.NaT
            return x
        except Exception:
            return pd.NaT

    try:
        if isinstance(s, pd.Series):
            return pd.to_datetime(s.map(_one), errors="coerce")
        return pd.to_datetime(pd.Series(s).map(_one), errors="coerce")
    except Exception:
        try:
            return pd.to_datetime(s, errors="coerce")
        except Exception:
            return pd.Series(pd.NaT, index=getattr(s, "index", None))


def _same_day_push_rows(df: pd.DataFrame, *, now_i: dt.datetime, label: str = "") -> pd.DataFrame:
    """Drop previous-day/future-day PUSH-like fallback rows before freshness/candidate selection."""
    if df is None or df.empty or "datetime" not in df.columns:
        return pd.DataFrame() if df is None else df
    try:
        x = df.copy()
        x["datetime"] = _normalize_dt_series(x["datetime"])
        before = len(x)
        day = today_date(now=now_i)
        x = x.dropna(subset=["datetime"])
        x = x[x["datetime"].dt.date == day].copy()
        if len(x) != before:
            logger.warning(
                "[PUSH FALLBACK SAME-DAY GUARD] dropped old rows label=%s before=%s after=%s today=%s latest_before=%s latest_after=%s",
                label,
                before,
                len(x),
                day,
                df["datetime"].max() if "datetime" in df.columns and not df.empty else None,
                x["datetime"].max() if not x.empty else None,
            )
        return x.reset_index(drop=True)
    except Exception:
        logger.exception("[PUSH FALLBACK SAME-DAY GUARD] failed label=%s", label)
        return pd.DataFrame()


def _latest_is_today(df: pd.DataFrame, *, now_i: dt.datetime, label: str = "") -> bool:
    try:
        if df is None or df.empty or "datetime" not in df.columns:
            return False
        dtv = _normalize_dt_series(df["datetime"])
        dtv = dtv.dropna()
        if dtv.empty:
            return False
        latest = dtv.max()
        ok = latest.date() == today_date(now=now_i)
        if not ok:
            logger.warning(
                "[PUSH FALLBACK SAME-DAY GUARD] reject candidate label=%s latest_dt=%s today=%s rows=%s",
                label,
                latest,
                today_date(now=now_i),
                len(df),
            )
        return bool(ok)
    except Exception:
        return False


def _safe_to_num(s):
    return pd.to_numeric(s, errors="coerce")


def _main_1m_raw_db_fallback_blocked(interval_i: int) -> bool:
    """main.pyの1分足ではraw/NAS DB fallbackを使わない。

    main_database.py がPUSH DB保存と重いsummary復元を担当するため、main.py側で
    pushYYYYMMDD.dbを読むとエントリー遅延になる。必要な場合だけ
    PUSH_SUMMARY_RAW_DB_FALLBACK_RUN_IN_MAIN=1 で戻せる。
    """
    try:
        if int(interval_i) != 1:
            return False
        if not _is_main_py_process():
            return False
        if _env_bool("PUSH_SUMMARY_RAW_DB_FALLBACK_RUN_IN_MAIN", False):
            return False
        if not _env_bool("SUMMARY_MAIN_DISABLE_RAW_DB_FALLBACK", True):
            return False
        return True
    except Exception:
        return False


def _load_recent_push_raw_summary(interval_i: int, *, now_i: dt.datetime) -> pd.DataFrame:
    if _main_1m_raw_db_fallback_blocked(interval_i):
        logger.warning(
            "[PUSH RAW DB FALLBACK] blocked in main.py interval=%s reason=main_1m_no_raw_db",
            interval_i,
        )
        return pd.DataFrame()
    if not _env_bool("PUSH_SUMMARY_RAW_DB_FALLBACK_ENABLED", True):
        return pd.DataFrame()
    path = _push_db_path()
    p = Path(path)
    if not p.exists():
        logger.warning("[PUSH RAW DB FALLBACK] db not found path=%s", path)
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
            else:
                where_parts.append("substr(datetime, 1, 10) = ?")
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
        logger.warning("[PUSH RAW DB FALLBACK] empty path=%s interval=%s since=%s", path, interval_i, since)
        return df

    try:
        df["datetime"] = _normalize_dt_series(df["datetime"])
        df = _same_day_push_rows(df, now_i=now_i, label=f"raw_db.interval{interval_i}")
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
        if pd.isna(latest_slot) or latest_slot.date() != today_date(now=now_i):
            logger.warning("[PUSH RAW DB FALLBACK] reject old latest_slot interval=%s latest_slot=%s today=%s", interval_i, latest_slot, today_date(now=now_i))
            return pd.DataFrame()
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
        out = _same_day_push_rows(out, now_i=now_i, label=f"raw_db.out.interval{interval_i}")
        if out.empty or not _latest_is_today(out, now_i=now_i, label=f"raw_db.out.interval{interval_i}"):
            return pd.DataFrame()
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
            "[PUSH RAW DB FALLBACK] loaded interval=%s rows=%s symbols=%s latest_dt=%s path=%s",
            interval_i,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
            path,
        )
        return out
    except Exception:
        logger.exception("[PUSH RAW DB FALLBACK] transform failed path=%s interval=%s", path, interval_i)
        return pd.DataFrame()


def _is_fresh_enough(df: pd.DataFrame, interval_i: int, now_i: dt.datetime, *, label: str = "") -> bool:
    try:
        if not _latest_is_today(df, now_i=now_i, label=label):
            return False
        ts = extract_latest_timestamp(df)
        if ts is None:
            return False
        return bool(is_fresh_timestamp(ts, interval_i, for_ranking=False, now=now_i))
    except Exception:
        return False


def _prepare_candidate(df: pd.DataFrame, interval_i: int, now_i: dt.datetime, label: str) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        x = normalize_df(df)
        x = filter_push_like_rows(x)
        x = _same_day_push_rows(x, now_i=now_i, label=label)
        if x.empty:
            return pd.DataFrame()
        x = _slot_aligned_latest_rows(x, interval=interval_i, now=now_i)
        x = _same_day_push_rows(x, now_i=now_i, label=f"{label}.slot")
        if not _is_fresh_enough(x, interval_i, now_i, label=label):
            return pd.DataFrame()
        return x.reset_index(drop=True)
    except Exception:
        logger.debug("[summary.fallback_loader] prepare candidate failed interval=%s label=%s", interval_i, label, exc_info=True)
        return pd.DataFrame()


def filter_push_like_rows(df: pd.DataFrame) -> pd.DataFrame:
    x = normalize_df(df)
    if x.empty or "source" not in x.columns:
        return x
    try:
        now_i = now_naive().replace(tzinfo=None, microsecond=0)
        if "datetime" in x.columns:
            x = _same_day_push_rows(x, now_i=now_i, label="filter_push_like_rows.input")
            if x.empty:
                return x
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
        out = _same_day_push_rows(out, now_i=now_i, label="filter_push_like_rows.output")
        logger.info(
            "[summary.fallback_loader] push-like filter rows=%s -> %s source_dist=%s",
            len(x),
            len(out),
            {} if out.empty else out["source"].astype(str).value_counts().head(10).to_dict(),
        )
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[summary.fallback_loader] push-like filter failed")
        return x


def _memory_push_fallback_candidates(interval: int, *, now: Optional[dt.datetime]) -> list[tuple[str, pd.DataFrame]]:
    candidates: list[tuple[str, Any]] = []
    for attr in (
        "push_df", "stream_data", "latest_push_df", "push_data", "push_snapshot_df",
        f"push_summary_{interval}min", f"push_summary_{interval}",
        f"latest_push_summary_{interval}min", f"latest_push_summary_{interval}",
        f"push_merged_summary_{interval}min", f"push_merged_summary_{interval}",
        f"merged_summary_{interval}min", f"merged_summary_{interval}",
        f"summary_{interval}m_df", f"latest_summary_{interval}m_df",
        "merged_summary",
    ):
        try:
            candidates.append((f"global_data.{attr}", safe_getattr(global_data, attr, None)))
        except Exception:
            pass

    for method_name in ("get_push_df", "get_merged_summary", "get_push_summary", "get_summary_history", "get_latest_summary"):
        try:
            fn = safe_getattr(global_data, method_name, None)
            if not callable(fn):
                continue
            try:
                if method_name == "get_push_df":
                    val = fn()
                elif method_name == "get_merged_summary":
                    val = fn(interval, source="push")
                else:
                    val = fn(interval)
                candidates.append((f"global_data.{method_name}", val))
            except TypeError:
                try:
                    candidates.append((f"global_data.{method_name}", fn(interval)))
                except Exception:
                    pass
        except Exception:
            pass

    normalized: list[tuple[str, pd.DataFrame]] = []
    for name, src in candidates:
        df = normalize_df(src)
        df = filter_push_like_rows(df)
        df = _slot_aligned_latest_rows(df, interval=interval, now=now)
        if not df.empty:
            normalized.append((name, df))
    return normalized


def _base_fallback_push_summary_df(interval: int, *, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    interval = int(interval)
    now = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    t0 = time.perf_counter()

    # main.py の 1分足ではNAS DB fallbackを読む前に、メモリ/前回mergedだけで即返す。
    if interval <= 1 and _is_main_entry_context() and _env_bool("SUMMARY_MAIN_FAST_FALLBACK_ENABLED", True):
        mem_candidates = _memory_push_fallback_candidates(interval, now=now)
        mem_df = select_best_candidate(mem_candidates, interval=interval, for_ranking=False, now=now) if mem_candidates else pd.DataFrame()
        if not mem_df.empty:
            logger.warning(
                "[summary.fallback_loader] main fast fallback memory return interval=%s rows=%s symbols=%s latest_dt=%s elapsed=%.3fs",
                interval,
                len(mem_df),
                symbols_count(mem_df),
                latest_dt_str(mem_df),
                time.perf_counter() - t0,
            )
            return mem_df
        if _main_1m_skip_db_fallback():
            logger.warning(
                "[summary.fallback_loader] main fast fallback memory empty; NAS DB fallback skipped interval=%s elapsed=%.3fs",
                interval,
                time.perf_counter() - t0,
            )
            return pd.DataFrame()

    candidates: list[tuple[str, Any]] = []
    db_sources = (
        "push_stream",
        "yahoo_pipeline",
        "incremental",
        "summary_recovery_push_1m",
        "summary_recovery_resample_3m",
        "summary_recovery_resample_5m",
        "summary_recovery",
    )

    for src in db_sources:
        try:
            candidates.append((f"db.stock_summary_{interval}min[{src}]", load_latest_summary_from_db(interval, source_filter=src, now=now)))
        except Exception:
            logger.debug("[summary.fallback_loader] db push-source fallback failed interval=%s src=%s", interval, src, exc_info=True)

    for attr in (f"push_summary_{interval}min", f"push_summary_{interval}", f"latest_push_summary_{interval}min", f"latest_push_summary_{interval}"):
        try:
            candidates.append((f"global_data.{attr}", safe_getattr(global_data, attr, None)))
        except Exception:
            pass

    for attr in ("push_summary_by_interval", "latest_push_summary_by_interval"):
        try:
            d = safe_getattr(global_data, attr, None)
            if isinstance(d, dict):
                candidates.append((f"global_data.{attr}[{interval}]", d.get(interval)))
        except Exception:
            pass

    normalized_candidates: list[tuple[str, pd.DataFrame]] = []
    for name, src in candidates:
        df = normalize_df(src)
        df = filter_push_like_rows(df)
        df = _slot_aligned_latest_rows(df, interval=interval, now=now)
        if not df.empty:
            normalized_candidates.append((name, df))

    df = select_best_candidate(normalized_candidates, interval=interval, for_ranking=False, now=now)
    if not df.empty:
        return df

    logger.warning("[summary.fallback_loader] fallback push summary empty interval=%s now=%s", interval, now)
    return pd.DataFrame()


def fallback_push_summary_df(interval: int, *, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    interval_i = int(interval)
    now_i = (now or now_naive()).replace(tzinfo=None, microsecond=0)

    if _main_1m_raw_db_fallback_blocked(interval_i):
        logger.warning(
            "[summary.fallback_loader] main 1m raw/db fallback disabled interval=%s",
            interval_i,
        )
        try:
            df0 = _base_fallback_push_summary_df(interval_i, now=now_i)
            df0 = _prepare_candidate(df0, interval_i, now_i, f"orig_fallback.main_no_raw.interval{interval_i}")
            if isinstance(df0, pd.DataFrame) and not df0.empty:
                logger.warning(
                    "[summary.fallback_loader] selected original memory fallback interval=%s rows=%s symbols=%s latest_dt=%s",
                    interval_i,
                    len(df0),
                    symbols_count(df0),
                    latest_dt_str(df0),
                )
                return df0.reset_index(drop=True)
        except Exception:
            logger.debug("[summary.fallback_loader] original memory fallback failed interval=%s", interval_i, exc_info=True)
        logger.warning(
            "[summary.fallback_loader] main 1m fallback empty without raw/db interval=%s now=%s",
            interval_i,
            now_i,
        )
        return pd.DataFrame()

    raw_df = _load_recent_push_raw_summary(interval_i, now_i=now_i)
    raw_df = _prepare_candidate(raw_df, interval_i, now_i, f"raw_db.interval{interval_i}")
    if isinstance(raw_df, pd.DataFrame) and not raw_df.empty:
        logger.warning(
            "[summary.fallback_loader] selected fresh push raw DB fallback interval=%s rows=%s symbols=%s latest_dt=%s",
            interval_i,
            len(raw_df),
            symbols_count(raw_df),
            latest_dt_str(raw_df),
        )
        return raw_df.reset_index(drop=True)

    try:
        df0 = _base_fallback_push_summary_df(interval_i, now=now_i)
        df0 = _prepare_candidate(df0, interval_i, now_i, f"orig_fallback.interval{interval_i}")
        if isinstance(df0, pd.DataFrame) and not df0.empty:
            logger.warning(
                "[summary.fallback_loader] selected original same-day fallback interval=%s rows=%s symbols=%s latest_dt=%s",
                interval_i,
                len(df0),
                symbols_count(df0),
                latest_dt_str(df0),
            )
            return df0.reset_index(drop=True)
    except Exception:
        logger.debug("[summary.fallback_loader] original push fallback failed interval=%s", interval_i, exc_info=True)

    candidates: list[tuple[str, pd.DataFrame]] = []
    if isinstance(raw_df, pd.DataFrame) and not raw_df.empty:
        candidates.append((f"db.push_raw[{interval_i}].patched", raw_df))
    for src in ("push", "SUMMARY", "summary", None):
        try:
            df = load_latest_summary_from_db(interval_i, source_filter=src, now=now_i)
            df = _prepare_candidate(df, interval_i, now_i, f"db.stock_summary_{interval_i}min[{src or '*'}]")
            if not df.empty:
                candidates.append((f"db.stock_summary_{interval_i}min[{src or '*'}].patched", df))
        except Exception:
            logger.debug("[summary.fallback_loader] patched push fallback source failed interval=%s src=%s", interval_i, src, exc_info=True)

    df = select_best_candidate(candidates, interval=interval_i, for_ranking=False, now=now_i)
    df = _same_day_push_rows(df, now_i=now_i, label=f"select_best.interval{interval_i}")
    if not df.empty and _is_fresh_enough(df, interval_i, now_i, label=f"select_best.interval{interval_i}"):
        logger.warning(
            "[summary.fallback_loader] patched push fallback selected interval=%s rows=%s symbols=%s latest_dt=%s",
            interval_i,
            len(df),
            symbols_count(df),
            latest_dt_str(df),
        )
        return df.reset_index(drop=True)

    logger.warning("[summary.fallback_loader] patched fallback push summary empty interval=%s now=%s", interval_i, now_i)
    return pd.DataFrame()


def fallback_ranking_summary_df(interval: int, *, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    interval = int(interval)
    now = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    candidates: list[tuple[str, Any]] = []

    try:
        from trading.ranking.ranking_summary_engine import get_latest_ranking_summary  # type: ignore
        candidates.append((f"ranking_cache.get_latest_ranking_summary({interval})", get_latest_ranking_summary(interval)))
    except Exception:
        logger.debug("[summary.fallback_loader] ranking cache fallback failed interval=%s", interval, exc_info=True)

    for attr in ("latest_ranking_summary_by_interval", "ranking_summary_by_interval", "ranking_summary_cache"):
        try:
            d = safe_getattr(global_data, attr, None)
            if isinstance(d, dict):
                candidates.append((f"global_data.{attr}[{interval}]", d.get(interval)))
        except Exception:
            pass

    for attr in (f"latest_ranking_summary_{interval}m", f"ranking_summary_{interval}m"):
        try:
            candidates.append((f"global_data.{attr}", safe_getattr(global_data, attr, None)))
        except Exception:
            pass

    normalized_candidates: list[tuple[str, pd.DataFrame]] = []
    for name, src in candidates:
        df = normalize_df(src)
        df = _slot_aligned_latest_rows(df, interval=interval, now=now)
        if not df.empty:
            normalized_candidates.append((name, df))

    df = select_best_candidate(normalized_candidates, interval=interval, for_ranking=True, now=now)
    if not df.empty:
        return df

    logger.warning("[summary.fallback_loader] fallback ranking summary empty interval=%s now=%s", interval, now)
    return pd.DataFrame()
