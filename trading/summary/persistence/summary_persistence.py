# ============================================================
# File   : trading/summary/persistence/summary_persistence.py
# Version: Ver3.2-FULL-PRESERVE-HARDENED-PRODUCTION-DIAG-FINAL
#          -DATETIME-NOTNULL-FIX-COMPAT
# ------------------------------------------------------------
# ✔ Ver3.1 完全互換（削除ゼロ）
# ✔ df_guard統合（非破壊追加）
# ✔ DB前完全防御（UNIQUE / NULL / dtype）
# ✔ symbol / datetime / OHLC保証
# ✔ NaN / inf 完全除去
# ✔ SQLite / DuckDB 安定化
# ✔ 本番完全版
# ✔ summary_saver_bulk Ver25 と整合
# ✔ save前後の診断ログ強化
# ✔ 例外時の interval / rows / tid 可視化
# ✔ 二重呼び出し切り分け補助
# ✔ lock_timeout_sec / skip_if_busy 互換対応
# ✔ datetime から date/time/start_time/end_time/time_range を補完
# ✔ NOT NULL(date) 対策
# ✔ 機能削除ゼロ
# ============================================================

from __future__ import annotations

import inspect
import logging
import threading
from typing import Any

import numpy as np
import pandas as pd

from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary

# ============================================================
# NEW DF GUARD（追加）
# ============================================================

from utils.df_guard.core import sanitize
from utils.df_guard.symbol_guard import ensure_symbol
from utils.df_guard.ohlc_guard import ensure_ohlc
from utils.df_guard.numeric_guard import sanitize_numeric, drop_na_rows
from utils.df_guard.index_guard import ensure_index

logger = logging.getLogger(__name__)


# ============================================================
# helper
# ============================================================

def _safe_len(df: Any) -> int:
    try:
        return 0 if df is None else len(df)
    except Exception:
        return 0


def _safe_cols(df: Any) -> int:
    try:
        return 0 if df is None else len(df.columns)
    except Exception:
        return 0


def _thread_ident() -> int:
    try:
        return threading.get_ident()
    except Exception:
        return -1


def _thread_name() -> str:
    try:
        return threading.current_thread().name
    except Exception:
        return "unknown"


def _resolve_caller_name(default: str = "unknown") -> str:
    """
    save_summary の呼び出し元を軽く可視化する。
    """
    try:
        stack = inspect.stack()
        if len(stack) >= 3:
            frame = stack[2]
            module = inspect.getmodule(frame.frame)
            mod_name = module.__name__ if module else ""
            fn_name = frame.function or ""
            if mod_name and fn_name:
                return f"{mod_name}.{fn_name}"
            if fn_name:
                return fn_name
    except Exception:
        pass
    return default


def _log_df_state(prefix: str, df: pd.DataFrame, interval: int) -> None:
    try:
        logger.debug(
            "%s interval=%s rows=%s cols=%s tid=%s thread=%s columns=%s",
            prefix,
            interval,
            _safe_len(df),
            _safe_cols(df),
            _thread_ident(),
            _thread_name(),
            list(df.columns)[:20] if isinstance(df, pd.DataFrame) else [],
        )
    except Exception:
        logger.exception("[summary_persistence] state log failed interval=%s", interval)


# ============================================================
# DataFrame safety（既存保持）
# ============================================================

def _safe_df(df):

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

    df = df.reset_index(drop=True)

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)

    return df


# ============================================================
# symbolname guarantee（既存保持）
# ============================================================

def _ensure_symbolname(df: pd.DataFrame):

    if df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    candidates = [
        "symbolname",
        "name",
        "symbol_name",
        "symbolname_jp",
    ]

    name_col = None

    for c in candidates:
        if c in df.columns:
            name_col = c
            break

    if name_col is None:
        df["symbolname"] = df["symbol"]
    else:
        df["symbolname"] = df[name_col]

    df["symbolname"] = (
        df["symbolname"]
        .fillna(df["symbol"])
        .astype(str)
    )

    df.loc[
        df["symbolname"].str.strip() == "",
        "symbolname",
    ] = df["symbol"]

    return df


# ============================================================
# datetime safety guard（強化版）
# ============================================================

def _ensure_datetime(df: pd.DataFrame):

    if df.empty:
        return df

    if "datetime" not in df.columns:
        return df

    df = df.copy()

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    try:
        if getattr(df["datetime"].dt, "tz", None) is not None:
            df["datetime"] = df["datetime"].dt.tz_localize(None)
    except Exception:
        pass

    before = len(df)

    df = df.dropna(subset=["datetime"])

    dropped = before - len(df)

    if dropped > 0:
        logger.warning(
            "[summary_persistence] dropped rows without datetime: %s",
            dropped
        )

    return df


def _normalize_none_like_text(s: pd.Series) -> pd.Series:
    try:
        x = s.astype("object")
        bad = x.astype(str).isin(["None", "nan", "NaN", "NaT", ""])
        x.loc[bad] = None
        return x
    except Exception:
        return s


def _ensure_required_datetime_fields(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    DBの NOT NULL(date) / 実用表示列 を満たすため、
    datetime から date/time/start_time/end_time/time_range を補完する。
    """
    if df.empty:
        return df

    df = df.copy()
    iv = max(int(interval), 1)

    if "datetime" not in df.columns:
        return df

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "end_time" not in df.columns:
        df["end_time"] = pd.NaT
    else:
        df["end_time"] = pd.to_datetime(df["end_time"], errors="coerce")

    if "start_time" not in df.columns:
        df["start_time"] = pd.NaT
    else:
        df["start_time"] = pd.to_datetime(df["start_time"], errors="coerce")

    # end_time は datetime で補完
    df["end_time"] = df["end_time"].fillna(df["datetime"])

    # start_time は end_time - interval 分
    need_start = df["start_time"].isna() & df["end_time"].notna()
    if need_start.any():
        df.loc[need_start, "start_time"] = (
            df.loc[need_start, "end_time"] - pd.to_timedelta(iv, unit="m")
        )

    # date 補完
    if "date" not in df.columns:
        df["date"] = pd.NaT
    try:
        date_dt = pd.to_datetime(df["date"], errors="coerce")
    except Exception:
        date_dt = pd.Series(pd.NaT, index=df.index)
    date_dt = date_dt.fillna(df["datetime"].dt.normalize())
    df["date"] = date_dt.dt.date

    # time 補完
    if "time" not in df.columns:
        df["time"] = pd.NaT
    df["time"] = df["datetime"].dt.time

    # time_range 補完
    if "time_range" not in df.columns:
        df["time_range"] = None

    df["time_range"] = _normalize_none_like_text(df["time_range"])

    start_txt = pd.to_datetime(df["start_time"], errors="coerce").dt.strftime("%H:%M")
    end_txt = pd.to_datetime(df["end_time"], errors="coerce").dt.strftime("%H:%M")
    computed_range = start_txt + " - " + end_txt

    bad_range = pd.isna(df["time_range"])
    if bad_range.any():
        df.loc[bad_range, "time_range"] = computed_range.loc[bad_range]

    # source が無ければ最低限入れる
    if "source" not in df.columns:
        df["source"] = "SUMMARY"

    # 補完後でも date 欠損は落とす
    before = len(df)
    df = df.dropna(subset=["datetime", "date"]).copy()
    dropped = before - len(df)
    if dropped > 0:
        logger.warning(
            "[summary_persistence] dropped rows after required datetime fill: %s",
            dropped,
        )

    return df


# ============================================================
# duplicate row guard（既存保持）
# ============================================================

def _remove_duplicates(df: pd.DataFrame):

    if df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    if "datetime" not in df.columns:
        return df

    df = (
        df
        .sort_values(["symbol", "datetime"], kind="mergesort")
        .drop_duplicates(["symbol", "datetime"], keep="last")
        .reset_index(drop=True)
    )

    return df


# ============================================================
# sanitize for DB（既存保持）
# ============================================================

def _sanitize_for_db(df):

    if df.empty:
        return df

    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.where(pd.notnull(df), None)

    return df


# ============================================================
# object dtype guard（既存保持）
# ============================================================

def _stabilize_object_columns(df):

    if df.empty:
        return df

    df = df.copy()

    for col in df.columns:
        if df[col].dtype == "object":
            try:
                if col in ("date", "time"):
                    continue
                df[col] = df[col].astype(str)
            except Exception:
                pass

    return df


# ============================================================
# NEW ENHANCED GUARD（追加・非破壊）
# ============================================================

def _enhance_guard(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    try:
        df = _safe_df(df)
        df = sanitize(df)
        df = ensure_symbol(df)
        df = ensure_ohlc(df)
        df = sanitize_numeric(df)
        df = ensure_index(df)

    except Exception:
        logger.exception("[summary_persistence] enhance guard failed")

    return df


# ============================================================
# final DB safety（強化版）
# ============================================================

def _final_db_guard(df, interval: int):

    if df.empty:
        return df

    try:
        df = _ensure_datetime(df)
        df = _ensure_required_datetime_fields(df, interval)
        df = _remove_duplicates(df)
        df = drop_na_rows(df, ["symbol", "datetime", "date"])
        df = _sanitize_for_db(df)
        df = _stabilize_object_columns(df)

    except Exception:
        logger.exception("[summary_persistence] final guard failed")

    return df


# ============================================================
# bulk upsert compat
# ============================================================

def _call_bulk_upsert_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    lock_timeout_sec=None,
    skip_if_busy=None,
):
    try:
        return bulk_upsert_summary(
            df.copy(),
            interval=interval,
            lock_timeout_sec=lock_timeout_sec,
            skip_if_busy=skip_if_busy,
        )
    except TypeError:
        # 旧シグネチャ互換
        return bulk_upsert_summary(
            df.copy(),
            interval
        )


# ============================================================
# main save（完全版）
# ============================================================

def save_summary(
    df: pd.DataFrame,
    interval: int,
    *args,
    lock_timeout_sec=None,
    skip_if_busy=None,
    **kwargs,
):

    caller = _resolve_caller_name()
    input_rows = _safe_len(df)

    try:
        interval = int(interval)

        logger.debug(
            "[summary_persistence] save start interval=%s rows=%s caller=%s tid=%s thread=%s lock_timeout_sec=%s skip_if_busy=%s",
            interval,
            input_rows,
            caller,
            _thread_ident(),
            _thread_name(),
            lock_timeout_sec,
            skip_if_busy,
        )

        df = _safe_df(df)

        if df.empty:
            logger.debug(
                "[summary_persistence] skip empty after _safe_df interval=%s caller=%s tid=%s",
                interval,
                caller,
                _thread_ident(),
            )
            return

        _log_df_state("[summary_persistence] after _safe_df", df, interval)

        df = _enhance_guard(df)

        if df.empty:
            logger.warning(
                "[summary_persistence] dataframe empty after enhance_guard interval=%s caller=%s tid=%s",
                interval,
                caller,
                _thread_ident(),
            )
            return

        _log_df_state("[summary_persistence] after _enhance_guard", df, interval)

        df = _ensure_symbolname(df)
        df = _final_db_guard(df, interval)

        if df.empty:
            logger.warning(
                "[summary_persistence] dataframe empty after guard interval=%s caller=%s tid=%s",
                interval,
                caller,
                _thread_ident(),
            )
            return

        _log_df_state("[summary_persistence] before bulk_upsert_summary", df, interval)

        _call_bulk_upsert_summary(
            df,
            interval=interval,
            lock_timeout_sec=lock_timeout_sec,
            skip_if_busy=skip_if_busy,
        )

        logger.debug(
            "[summary_persistence] saved interval=%s rows=%s caller=%s tid=%s thread=%s",
            interval,
            len(df),
            caller,
            _thread_ident(),
            _thread_name(),
        )

    except Exception:

        logger.exception(
            "[summary_persistence] save failed interval=%s rows=%s caller=%s tid=%s thread=%s",
            interval,
            input_rows,
            caller,
            _thread_ident(),
            _thread_name(),
        )
        raise