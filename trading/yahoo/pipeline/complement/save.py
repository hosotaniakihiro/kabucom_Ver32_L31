# ============================================================
# File   : trading/yahoo/pipeline/complement/save.py
# Version: YAHOO-COMPLEMENT-SAVE-REV5.0-VERIFY-AND-DIRECT-FALLBACK
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完サマリーの保存・cache更新。
#
# 【REV5.0 修正】
#   ✔ recovery.persistence.upsert_summary_df() が None を返した場合、保存成功扱いしない
#   ✔ 既存保存関数が「成功件数不明」の場合は DB実在確認を行う
#   ✔ DB確認できない場合は direct SQLite DELETE→INSERT fallback を必ず実行
#   ✔ 1m/3m/5m の summaryYYYYMMDD.db 保存結果をログで確認しやすくする
#   ✔ tableに存在しない列は除外し、既存schemaに合わせて保存する
#   ✔ database is locked 時は短時間待ってから direct fallback へ移る
#
# 【重要】
#   旧版は ret is None の場合に len(out) を saved_rows とみなしていたため、
#   実際には summary DB に保存されていないのに「保存成功」扱いになり、
#   direct SQLite fallback が実行されない可能性があった。
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import logging
import os
import sqlite3
import time
from typing import Callable, Optional

import pandas as pd

from .constants import (
    YAHOO_SUMMARY_LOCK_TIMEOUT_SEC,
    YAHOO_SUMMARY_SKIP_IF_BUSY,
    summary_table_for_interval,
)
from .db import get_summary_db_path
from .normalize import safe_df

logger = logging.getLogger(__name__)

try:
    from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
except Exception:  # pragma: no cover
    bulk_upsert_summary = None  # type: ignore

try:
    from global_state import global_data
except Exception:  # pragma: no cover
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None


# ============================================================
# callable resolution
# ============================================================

def _resolve_callable(candidates: list[tuple[str, str]]) -> tuple[Optional[Callable], Optional[str]]:
    for mod_name, fn_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                name = f"{mod_name}.{fn_name}"
                logger.info("[YAHOO SAVE] resolved callable %s", name)
                return fn, name
        except Exception:
            logger.debug("[YAHOO SAVE] resolve failed mod=%s fn=%s", mod_name, fn_name, exc_info=True)
    return None, None


_finalize_for_upsert_fn, _finalize_for_upsert_name = _resolve_callable(
    [
        ("trading.summary.recovery.persistence", "finalize_for_upsert"),
    ]
)

_upsert_summary_df_fn, _upsert_summary_df_name = _resolve_callable(
    [
        ("trading.summary.recovery.persistence", "upsert_summary_df"),
    ]
)

_update_global_cache_fn, _update_global_cache_name = _resolve_callable(
    [
        ("trading.summary.recovery.persistence", "update_global_cache"),
    ]
)


# ============================================================
# dataframe helpers
# ============================================================

def finalize_for_upsert_if_possible(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    if callable(_finalize_for_upsert_fn):
        attempts = [
            lambda: _finalize_for_upsert_fn(out, interval=int(interval)),
            lambda: _finalize_for_upsert_fn(out, int(interval)),
            lambda: _finalize_for_upsert_fn(df=out, interval=int(interval)),
        ]

        for caller in attempts:
            try:
                ret = caller()
                if isinstance(ret, pd.DataFrame):
                    logger.info(
                        "[YAHOO SAVE] finalize_for_upsert done interval=%s rows=%s backend=%s",
                        interval,
                        len(ret),
                        _finalize_for_upsert_name,
                    )
                    return ret
            except TypeError:
                continue
            except Exception:
                logger.exception(
                    "[YAHOO SAVE] finalize_for_upsert backend failed interval=%s backend=%s",
                    interval,
                    _finalize_for_upsert_name,
                )
                break

    return out


def _to_sqlite_value(v):
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass

    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d")

    return v


def _normalize_for_sqlite(df: pd.DataFrame) -> pd.DataFrame:
    work = safe_df(df)
    if work.empty:
        return work

    if "symbol" in work.columns:
        work["symbol"] = work["symbol"].astype(str).str.replace(".0", "", regex=False).str.strip()

    if "datetime" in work.columns:
        work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")

    if "date" in work.columns:
        # date型/文字列どちらでも yyyy-mm-dd に寄せる。失敗時は元文字列。
        try:
            dts = pd.to_datetime(work["date"], errors="coerce")
            formatted = dts.dt.strftime("%Y-%m-%d")
            work["date"] = formatted.where(formatted.notna(), work["date"].astype(str))
        except Exception:
            work["date"] = work["date"].astype(str)

    if "time_range" in work.columns:
        work["time_range"] = work["time_range"].astype(str)

    if "time" in work.columns:
        work["time"] = work["time"].astype(str)

    if "last_update" in work.columns:
        try:
            work["last_update"] = pd.to_datetime(work["last_update"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            work["last_update"] = work["last_update"].astype(str)

    return work


def _key_columns_for_table(work: pd.DataFrame, *, interval: int, table_cols: list[str]) -> list[str]:
    cols = set(work.columns)
    table_col_set = set(table_cols)

    # 1分足は symbol + datetime が最優先。
    if int(interval) == 1 and {"symbol", "datetime"}.issubset(cols) and {"symbol", "datetime"}.issubset(table_col_set):
        return ["symbol", "datetime"]

    # 3分/5分の既存schemaは date + time_range 型もあり得る。
    if {"symbol", "date", "time_range"}.issubset(cols) and {"symbol", "date", "time_range"}.issubset(table_col_set):
        return ["symbol", "date", "time_range"]

    if {"symbol", "datetime"}.issubset(cols) and {"symbol", "datetime"}.issubset(table_col_set):
        return ["symbol", "datetime"]

    if {"symbol", "date", "time"}.issubset(cols) and {"symbol", "date", "time"}.issubset(table_col_set):
        return ["symbol", "date", "time"]

    return []


def _detect_date_yyyymmdd(df: pd.DataFrame) -> Optional[str]:
    """
    保存対象DFから summaryYYYYMMDD.db の日付を推定する。
    get_summary_db_path() は既定で今日を見るが、前日/昼休み処理ではDF日付を優先する。
    """
    out = safe_df(df)
    if out.empty:
        return None

    try:
        if "datetime" in out.columns:
            s = pd.to_datetime(out["datetime"], errors="coerce").dropna()
            if not s.empty:
                return pd.Timestamp(s.max()).strftime("%Y%m%d")
        if "date" in out.columns:
            s = pd.to_datetime(out["date"], errors="coerce").dropna()
            if not s.empty:
                return pd.Timestamp(s.max()).strftime("%Y%m%d")
    except Exception:
        return None

    return None


# ============================================================
# DB verification / direct upsert
# ============================================================

def _count_existing_rows_for_keys(df: pd.DataFrame, *, interval: int, db_path: str) -> int:
    """
    保存後の実在確認。
    全件を見ると重いので、最大50 keyだけ確認する。
    """
    work = _normalize_for_sqlite(df)
    if work.empty:
        return 0

    table = summary_table_for_interval(interval)

    if not os.path.exists(db_path):
        return 0

    con = None
    try:
        con = sqlite3.connect(str(db_path), timeout=5.0)
        con.execute("PRAGMA busy_timeout = 5000")

        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return 0

        table_cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
        key_cols = _key_columns_for_table(work, interval=interval, table_cols=table_cols)
        if not key_cols:
            return 0

        work = work.dropna(subset=key_cols).drop_duplicates(subset=key_cols, keep="last")
        if work.empty:
            return 0

        sql = f"SELECT 1 FROM {table} WHERE " + " AND ".join([f"{c}=?" for c in key_cols]) + " LIMIT 1"
        count = 0
        for _, row in work[key_cols].head(50).iterrows():
            params = tuple(_to_sqlite_value(row[c]) for c in key_cols)
            if con.execute(sql, params).fetchone() is not None:
                count += 1

        return count

    except Exception:
        logger.debug("[YAHOO SAVE] verify count failed interval=%s db=%s", interval, db_path, exc_info=True)
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _direct_sqlite_upsert_summary_df(df: pd.DataFrame, *, interval: int, db_path: Optional[str] = None) -> int:
    """
    summary_saver_bulk / recovery.persistence が使えない、または保存確認できない場合の最終保険。

    設計:
      - 既存summary DBの実カラムだけに絞る
      - 1min は symbol+datetime、3min/5min は symbol+date+time_range を優先キーにする
      - DELETE → INSERT なので、既存PUSH行をYahoo source行で確実に置き換える
      - source は呼び出し元で summary_recovery_yahoo_* が入っている前提
    """
    out = safe_df(df)
    if out.empty:
        return 0

    table = summary_table_for_interval(interval)
    db_path = db_path or get_summary_db_path(date_yyyymmdd=_detect_date_yyyymmdd(out))

    con = None
    try:
        if not os.path.exists(str(db_path)):
            logger.error("[YAHOO SAVE][DIRECT] summary db not found interval=%s table=%s db=%s", interval, table, db_path)
            return 0

        timeout = max(float(YAHOO_SUMMARY_LOCK_TIMEOUT_SEC), 15.0)
        con = sqlite3.connect(str(db_path), timeout=timeout)
        con.execute("PRAGMA busy_timeout = 15000")
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
        except Exception:
            pass

        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            logger.error("[YAHOO SAVE][DIRECT] summary table not found interval=%s table=%s db=%s", interval, table, db_path)
            return 0

        table_cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
        if not table_cols:
            logger.error("[YAHOO SAVE][DIRECT] no table columns interval=%s table=%s db=%s", interval, table, db_path)
            return 0

        work = _normalize_for_sqlite(out)

        if "last_update" in table_cols and "last_update" not in work.columns:
            work["last_update"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

        if "source" in table_cols and "source" not in work.columns:
            work["source"] = "summary_recovery_yahoo_1m" if int(interval) == 1 else f"summary_recovery_yahoo_resample_{int(interval)}m"

        cols = [c for c in table_cols if c in work.columns and c != "id"]
        if not cols:
            logger.error("[YAHOO SAVE][DIRECT] no matching columns interval=%s table=%s df_cols=%s", interval, table, list(work.columns))
            return 0

        key_cols = _key_columns_for_table(work, interval=interval, table_cols=table_cols)
        if not key_cols:
            logger.error("[YAHOO SAVE][DIRECT] no usable key columns interval=%s table=%s df_cols=%s table_cols=%s", interval, table, list(work.columns), table_cols)
            return 0

        work = work.dropna(subset=key_cols).drop_duplicates(subset=key_cols, keep="last").reset_index(drop=True)
        if work.empty:
            logger.warning("[YAHOO SAVE][DIRECT] no rows after key cleanup interval=%s table=%s key=%s", interval, table, key_cols)
            return 0

        # 既存行削除。PUSH由来行もYahoo由来行で置き換える。
        delete_sql = f"DELETE FROM {table} WHERE " + " AND ".join([f"{c}=?" for c in key_cols])
        delete_params = [
            tuple(_to_sqlite_value(row[c]) for c in key_cols)
            for _, row in work[key_cols].iterrows()
        ]
        con.executemany(delete_sql, delete_params)

        placeholders = ",".join(["?"] * len(cols))
        col_sql = ",".join(cols)
        insert_sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"
        records = [
            tuple(_to_sqlite_value(row.get(c)) for c in cols)
            for _, row in work[cols].iterrows()
        ]
        con.executemany(insert_sql, records)
        con.commit()

        logger.info(
            "[YAHOO SAVE][DIRECT] summary sqlite upsert done interval=%s table=%s rows=%s keys=%s db=%s source=%s latest=%s cols=%s",
            interval,
            table,
            len(records),
            key_cols,
            db_path,
            (work["source"].iloc[0] if "source" in work.columns and not work.empty else None),
            (work["datetime"].max() if "datetime" in work.columns and not work.empty else None),
            len(cols),
        )
        return int(len(records))

    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            logger.warning("[YAHOO SAVE][DIRECT] database busy/locked interval=%s table=%s db=%s err=%s", interval, table, db_path, e)
        else:
            logger.exception("[YAHOO SAVE][DIRECT] sqlite operational error interval=%s table=%s db=%s", interval, table, db_path)
        if con is not None:
            try:
                con.rollback()
            except Exception:
                pass
        return 0

    except Exception:
        if con is not None:
            try:
                con.rollback()
            except Exception:
                pass
        logger.exception("[YAHOO SAVE][DIRECT] direct sqlite upsert failed interval=%s table=%s db=%s", interval, table, db_path)
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


# ============================================================
# save APIs
# ============================================================

def _parse_saved_count(ret) -> Optional[int]:
    """
    保存関数の戻り値から件数を読む。
    None は「成功件数不明」であり、成功扱いしない。
    """
    if ret is None:
        return None
    try:
        return int(ret)
    except Exception:
        return None


def save_summary_df(df: pd.DataFrame, *, interval: int) -> int:
    out = safe_df(df)
    if out.empty:
        return 0

    date_yyyymmdd = _detect_date_yyyymmdd(out)
    db_path = get_summary_db_path(date_yyyymmdd=date_yyyymmdd)
    table = summary_table_for_interval(interval)

    logger.info(
        "[YAHOO SAVE] start interval=%s table=%s rows=%s db=%s source=%s latest=%s",
        interval,
        table,
        len(out),
        db_path,
        (out["source"].iloc[0] if "source" in out.columns and not out.empty else None),
        (pd.to_datetime(out["datetime"], errors="coerce").max() if "datetime" in out.columns and not out.empty else None),
    )

    # --------------------------------------------------------
    # 1. recovery.persistence.upsert_summary_df を試す
    # --------------------------------------------------------
    if callable(_upsert_summary_df_fn):
        attempts = [
            lambda: _upsert_summary_df_fn(out, interval=int(interval)),
            lambda: _upsert_summary_df_fn(out, int(interval)),
            lambda: _upsert_summary_df_fn(df=out, interval=int(interval)),
        ]

        for caller in attempts:
            try:
                ret = caller()
                parsed = _parse_saved_count(ret)

                if parsed is not None and parsed > 0:
                    verified = _count_existing_rows_for_keys(out, interval=interval, db_path=db_path)
                    if verified > 0:
                        logger.info(
                            "[YAHOO SAVE] summary upsert verified via recovery.persistence interval=%s rows=%s saved=%s verified_sample=%s backend=%s",
                            interval,
                            len(out),
                            parsed,
                            verified,
                            _upsert_summary_df_name,
                        )
                        return parsed

                    logger.warning(
                        "[YAHOO SAVE] recovery.persistence returned saved=%s but DB verify=0 -> direct fallback interval=%s db=%s",
                        parsed,
                        interval,
                        db_path,
                    )
                    break

                logger.warning(
                    "[YAHOO SAVE] recovery.persistence returned unknown/zero ret=%r -> direct fallback interval=%s rows=%s backend=%s",
                    ret,
                    interval,
                    len(out),
                    _upsert_summary_df_name,
                )
                break

            except TypeError:
                continue
            except Exception:
                logger.exception(
                    "[YAHOO SAVE] recovery.persistence upsert failed interval=%s rows=%s backend=%s",
                    interval,
                    len(out),
                    _upsert_summary_df_name,
                )
                break

    # --------------------------------------------------------
    # 2. bulk_upsert_summary を試す
    # --------------------------------------------------------
    if callable(bulk_upsert_summary):
        try:
            saved_rows = bulk_upsert_summary(
                out,
                interval=interval,
                lock_timeout_sec=YAHOO_SUMMARY_LOCK_TIMEOUT_SEC,
                skip_if_busy=YAHOO_SUMMARY_SKIP_IF_BUSY,
            )
            parsed = _parse_saved_count(saved_rows)

            if parsed is not None and parsed > 0:
                verified = _count_existing_rows_for_keys(out, interval=interval, db_path=db_path)
                if verified > 0:
                    logger.info(
                        "[YAHOO SAVE] summary upsert verified via bulk interval=%s rows=%s saved=%s verified_sample=%s source=%s",
                        interval,
                        len(out),
                        parsed,
                        verified,
                        (out["source"].iloc[0] if "source" in out.columns and not out.empty else None),
                    )
                    return parsed

                logger.warning(
                    "[YAHOO SAVE] bulk returned saved=%s but DB verify=0 -> direct fallback interval=%s db=%s",
                    parsed,
                    interval,
                    db_path,
                )
            else:
                logger.warning(
                    "[YAHOO SAVE] summary bulk returned unknown/0/busy interval=%s rows=%s ret=%r timeout=%.1fs -> direct sqlite fallback",
                    interval,
                    len(out),
                    saved_rows,
                    YAHOO_SUMMARY_LOCK_TIMEOUT_SEC,
                )

        except TypeError:
            try:
                result = bulk_upsert_summary(out, interval=interval)
                parsed = _parse_saved_count(result)
                if parsed is not None and parsed > 0:
                    verified = _count_existing_rows_for_keys(out, interval=interval, db_path=db_path)
                    if verified > 0:
                        return parsed
                logger.warning("[YAHOO SAVE] bulk simple call unverified/zero -> direct fallback interval=%s ret=%r", interval, result)
            except Exception:
                logger.exception("[YAHOO SAVE] bulk_upsert simple fallback failed interval=%s", interval)

        except Exception:
            logger.exception("[YAHOO SAVE] bulk_upsert failed interval=%s", interval)

    # --------------------------------------------------------
    # 3. 最終保険: 直接SQLiteへ DELETE→INSERT
    # --------------------------------------------------------
    direct_saved = _direct_sqlite_upsert_summary_df(out, interval=interval, db_path=db_path)
    if direct_saved > 0:
        verified = _count_existing_rows_for_keys(out, interval=interval, db_path=db_path)
        logger.info(
            "[YAHOO SAVE] direct fallback final result interval=%s rows=%s saved=%s verified_sample=%s db=%s",
            interval,
            len(out),
            direct_saved,
            verified,
            db_path,
        )
        return direct_saved

    logger.error(
        "[YAHOO SAVE] all summary upsert backends failed interval=%s table=%s rows=%s db=%s source=%s latest=%s",
        interval,
        table,
        len(out),
        db_path,
        (out["source"].iloc[0] if "source" in out.columns and not out.empty else None),
        (pd.to_datetime(out["datetime"], errors="coerce").max() if "datetime" in out.columns and not out.empty else None),
    )
    return 0


def update_global_cache_if_possible(df: pd.DataFrame, *, interval: int) -> None:
    out = safe_df(df)
    if out.empty:
        return

    try:
        if callable(_update_global_cache_fn):
            attempts = [
                lambda: _update_global_cache_fn(out, interval=int(interval)),
                lambda: _update_global_cache_fn(out, int(interval)),
                lambda: _update_global_cache_fn(df=out, interval=int(interval)),
            ]
            for caller in attempts:
                try:
                    caller()
                    logger.info(
                        "[YAHOO SAVE] global cache updated via persistence interval=%s rows=%s backend=%s",
                        interval,
                        len(out),
                        _update_global_cache_name,
                    )
                    return
                except TypeError:
                    continue
                except Exception:
                    logger.exception("[YAHOO SAVE] update_global_cache backend failed interval=%s", interval)
                    break

        if global_data is not None and hasattr(global_data, "set_merged_summary"):
            global_data.set_merged_summary(int(interval), out, source="yahoo")
            logger.info("[YAHOO SAVE] global cache updated set_merged_summary interval=%s rows=%s", interval, len(out))
            return

        if global_data is not None and hasattr(global_data, "set_push_merged_summary"):
            global_data.set_push_merged_summary(int(interval), out)
            logger.info("[YAHOO SAVE] global cache updated set_push_merged_summary interval=%s rows=%s", interval, len(out))
            return

    except Exception:
        logger.exception("[YAHOO SAVE] global cache update failed interval=%s", interval)


def finalize_and_save(
    df: pd.DataFrame,
    *,
    interval: int,
    save: bool = True,
    update_cache: bool = True,
) -> tuple[pd.DataFrame, int]:
    out = safe_df(df)
    if out.empty:
        return out, 0

    finalized = finalize_for_upsert_if_possible(out, interval=interval)

    saved_rows = 0
    if save:
        saved_rows = save_summary_df(finalized, interval=interval)

    if update_cache and saved_rows > 0:
        update_global_cache_if_possible(finalized, interval=interval)
    elif update_cache and saved_rows <= 0:
        logger.warning("[YAHOO SAVE] cache update skipped because saved_rows=0 interval=%s rows=%s", interval, len(finalized))

    return finalized, saved_rows


__all__ = [
    "finalize_for_upsert_if_possible",
    "save_summary_df",
    "_direct_sqlite_upsert_summary_df",
    "update_global_cache_if_possible",
    "finalize_and_save",
]
