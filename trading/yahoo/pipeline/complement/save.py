# ============================================================
# File   : trading/yahoo/pipeline/complement/save.py
# Version: PRODUCTION-STABLE-REV4.3-YAHOO-COMPLEMENT-SAVE-DIRECT-SQLITE
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完サマリーの保存・cache更新
#
# 【主な機能】
#   - finalize_for_upsert 呼び出し
#   - upsert_summary_df 優先保存
#   - bulk_upsert_summary fallback
#   - short-timeout + skip_if_busy
#   - global_data cache更新
#
# 【重要】
#   - recovery.persistence.finalize_for_upsert を通すことで
#     summary DB列に正規化される
#   - symbol+datetime UPSERTによりPUSH由来行をYahoo由来行で上書きする
# ============================================================

from __future__ import annotations

import importlib
import logging
import sqlite3
import datetime as dt
from typing import Callable, Optional

import pandas as pd

from .constants import (
    YAHOO_SUMMARY_LOCK_TIMEOUT_SEC,
    YAHOO_SUMMARY_SKIP_IF_BUSY,
    summary_table_for_interval,
)
from .normalize import safe_df
from .db import get_summary_db_path

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

    if isinstance(v, (dt.datetime, dt.date)):
        try:
            return v.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(v)

    return v


def _direct_sqlite_upsert_summary_df(df: pd.DataFrame, *, interval: int) -> int:
    """
    summary_saver_bulk / recovery.persistence が使えない、または0件返却した場合の最終保険。

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
    db_path = get_summary_db_path()

    con = None
    try:
        con = sqlite3.connect(str(db_path), timeout=max(float(YAHOO_SUMMARY_LOCK_TIMEOUT_SEC), 15.0))
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

        work = out.copy()

        if "datetime" in work.columns:
            work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        if "date" in work.columns:
            work["date"] = work["date"].astype(str)
        if "time_range" in work.columns:
            work["time_range"] = work["time_range"].astype(str)
        if "symbol" in work.columns:
            work["symbol"] = work["symbol"].astype(str).str.replace(".0", "", regex=False).str.strip()

        if "last_update" in table_cols and "last_update" not in work.columns:
            work["last_update"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

        cols = [c for c in table_cols if c in work.columns and c != "id"]
        if not cols:
            logger.error("[YAHOO SAVE][DIRECT] no matching columns interval=%s table=%s df_cols=%s", interval, table, list(work.columns))
            return 0

        key_cols: list[str]
        if int(interval) == 1 and {"symbol", "datetime"}.issubset(work.columns):
            key_cols = ["symbol", "datetime"]
        elif {"symbol", "date", "time_range"}.issubset(work.columns):
            key_cols = ["symbol", "date", "time_range"]
        elif {"symbol", "datetime"}.issubset(work.columns):
            key_cols = ["symbol", "datetime"]
        else:
            logger.error("[YAHOO SAVE][DIRECT] no usable key columns interval=%s table=%s cols=%s", interval, table, list(work.columns))
            return 0

        work = work.dropna(subset=key_cols).drop_duplicates(subset=key_cols, keep="last").reset_index(drop=True)
        if work.empty:
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
            "[YAHOO SAVE][DIRECT] summary sqlite upsert done interval=%s table=%s rows=%s keys=%s db=%s source=%s latest=%s",
            interval,
            table,
            len(records),
            key_cols,
            db_path,
            (work["source"].iloc[0] if "source" in work.columns and not work.empty else None),
            (work["datetime"].max() if "datetime" in work.columns and not work.empty else None),
        )
        return int(len(records))

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


def save_summary_df(df: pd.DataFrame, *, interval: int) -> int:
    out = safe_df(df)
    if out.empty:
        return 0

    primary_saved = 0

    # recovery.persistence.upsert_summary_df を優先
    if callable(_upsert_summary_df_fn):
        attempts = [
            lambda: _upsert_summary_df_fn(out, interval=int(interval)),
            lambda: _upsert_summary_df_fn(out, int(interval)),
            lambda: _upsert_summary_df_fn(df=out, interval=int(interval)),
        ]

        for caller in attempts:
            try:
                ret = caller()
                primary_saved = len(out) if ret is None else int(ret) if str(ret).isdigit() else len(out)
                logger.info(
                    "[YAHOO SAVE] summary upsert done via recovery.persistence interval=%s rows=%s saved=%s backend=%s",
                    interval,
                    len(out),
                    primary_saved,
                    _upsert_summary_df_name,
                )
                if primary_saved > 0:
                    return primary_saved
                break
            except TypeError:
                continue
            except Exception:
                logger.exception(
                    "[YAHOO SAVE] recovery.persistence upsert failed interval=%s rows=%s",
                    interval,
                    len(out),
                )
                break

    # fallback: bulk_upsert_summary
    if callable(bulk_upsert_summary):
        try:
            saved_rows = bulk_upsert_summary(
                out,
                interval=interval,
                lock_timeout_sec=YAHOO_SUMMARY_LOCK_TIMEOUT_SEC,
                skip_if_busy=YAHOO_SUMMARY_SKIP_IF_BUSY,
            )
            if saved_rows is None:
                saved_rows = 0

            primary_saved = int(saved_rows)

            if primary_saved > 0:
                logger.info(
                    "[YAHOO SAVE] summary upsert done via bulk interval=%s rows=%s saved=%s source=%s",
                    interval,
                    len(out),
                    primary_saved,
                    (out["source"].iloc[0] if "source" in out.columns and not out.empty else None),
                )
                return primary_saved

            logger.warning(
                "[YAHOO SAVE] summary bulk returned 0/busy interval=%s rows=%s timeout=%.1fs -> direct sqlite fallback",
                interval,
                len(out),
                YAHOO_SUMMARY_LOCK_TIMEOUT_SEC,
            )

        except TypeError:
            try:
                result = bulk_upsert_summary(out, interval=interval)
                if result is None:
                    primary_saved = len(out)
                else:
                    try:
                        primary_saved = int(result)
                    except Exception:
                        primary_saved = len(out)
                if primary_saved > 0:
                    return primary_saved
            except Exception:
                logger.exception("[YAHOO SAVE] bulk_upsert fallback failed interval=%s", interval)

        except Exception:
            logger.exception("[YAHOO SAVE] bulk_upsert failed interval=%s", interval)

    # 最終保険: 直接SQLiteへ DELETE→INSERT
    direct_saved = _direct_sqlite_upsert_summary_df(out, interval=interval)
    if direct_saved > 0:
        return direct_saved

    logger.error("[YAHOO SAVE] all summary upsert backends failed interval=%s rows=%s", interval, len(out))
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

    return finalized, saved_rows


__all__ = [
    "finalize_for_upsert_if_possible",
    "save_summary_df",
    "_direct_sqlite_upsert_summary_df",
    "update_global_cache_if_possible",
    "finalize_and_save",
]