# ============================================================
# File   : ats/ats_ranking/db_loader.py
# Version: Ver1.1-ATS-RANKING-DB-LOADER-ROBUST-PRIMARY
# ------------------------------------------------------------
# 【概要】
#   ATSランキング候補生成用のランキングDB読み込みモジュール
#
# 【主な機能】
#   - 当日 ranking DB から ATS 候補元データを読み込む
#   - ranking_snapshot_1min を最優先で使用
#   - ranking_raw_1min / ranking / category tables を fallback として使用
#   - global_data.summary_cache からの fallback も維持
#   - normalizer 側の標準化に失敗しても、最低限の列補完で救済
#
# 【重要】
#   以前の実装では、
#     - ranking_snapshot_1min が存在する
#     - ranking_raw_1min が存在する
#     - category tables が存在する
#   状態でも、_standardize_snapshot_df が空を返した場合に
#   詳細理由が見えず、最後に
#
#       [ATS RANKING] no usable ranking tables found
#
#   となっていた。
#
#   本版では、各テーブルの raw rows / cols / symbol数 / 日時列候補をログに出し、
#   標準化に失敗した場合でも fallback 標準化で救済する。
#
# 【優先順位】
#   1. ranking_snapshot_1min
#   2. ranking_raw_1min
#   3. ranking
#   4. category tables
#   5. summary_cache fallback
#
# 【注意】
#   - スコア計算そのものはここでは行わない
#   - ここでは ATS 候補生成に必要な最低限のランキング列を揃える
#   - volume_speed / price_delta_1m 等が無い場合は 0 補完するが、
#     それがスコア0の原因にならないよう、上流のランキング保存側で
#     差分計算を行うことが望ましい
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from typing import Optional, Iterable

import numpy as np
import pandas as pd

from global_state import global_data

from .constants import CATEGORY_TABLE_SPECS
from .db_path import get_usable_ranking_db_path
from .normalizer import (
    _infer_common_columns,
    _safe_symbol,
    _safe_numeric_series,
    _normalize_market_type,
    _table_exists,
    _list_tables,
    _safe_read_sql,
    _standardize_snapshot_df,
    _standardize_summary_fallback,
)

logger = logging.getLogger(__name__)


# ============================================================
# constants
# ============================================================

PRIMARY_TABLES_ORDER = (
    "ranking_snapshot_1min",
    "ranking_raw_1min",
    "ranking",
)

DATETIME_CANDIDATES = (
    "snapshot_time",
    "datetime",
    "inserted_at",
    "created_at",
    "time",
    "timestamp",
    "CurrentPriceTime",
    "current_price_time",
)

PRICE_CANDIDATES = (
    "current_price",
    "price",
    "CurrentPrice",
    "value",
)

VOLUME_CANDIDATES = (
    "trading_volume",
    "volume",
    "TradingVolume",
)

TRADING_VALUE_CANDIDATES = (
    "trading_value",
    "turnover",
    "Turnover",
    "TradingValue",
)

RANK_TYPE_CANDIDATES = (
    "rank_type",
    "ranking_type",
    "category",
    "CategoryName",
)

RANK_POSITION_CANDIDATES = (
    "rank_position",
    "rank",
    "No",
    "AverageRanking",
)

SYMBOLNAME_CANDIDATES = (
    "symbolname",
    "symbol_name",
    "SymbolName",
    "name",
)


# ============================================================
# generic helpers
# ============================================================

def _first_existing_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    if df is None or df.empty:
        return None

    cols = set(map(str, df.columns))
    for c in candidates:
        if c in cols:
            return c
    return None


def _copy_series_if_exists(
    df: pd.DataFrame,
    target_col: str,
    candidates: Iterable[str],
    *,
    default=None,
) -> pd.DataFrame:
    if target_col in df.columns:
        return df

    src = _first_existing_col(df, candidates)
    if src is not None:
        df[target_col] = df[src]
    else:
        df[target_col] = default

    return df


def _safe_dt_series(s: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        return pd.Series(pd.NaT, index=s.index)


def _log_table_profile(
    *,
    label: str,
    table_name: str,
    df: pd.DataFrame,
    db_path: Optional[str] = None,
) -> None:
    try:
        rows = len(df) if isinstance(df, pd.DataFrame) else 0
        cols = list(df.columns) if isinstance(df, pd.DataFrame) else []

        symbol_count = 0
        if isinstance(df, pd.DataFrame) and "symbol" in df.columns:
            try:
                symbol_count = int(df["symbol"].astype(str).replace("", np.nan).dropna().nunique())
            except Exception:
                symbol_count = 0

        dt_info = {}
        if isinstance(df, pd.DataFrame):
            for c in DATETIME_CANDIDATES:
                if c in df.columns:
                    try:
                        dts = pd.to_datetime(df[c], errors="coerce")
                        dt_info[c] = {
                            "non_null": int(dts.notna().sum()),
                            "min": str(dts.min()) if dts.notna().any() else None,
                            "max": str(dts.max()) if dts.notna().any() else None,
                        }
                    except Exception:
                        dt_info[c] = {"error": True}

        logger.info(
            "[ATS RANKING] table profile label=%s table=%s rows=%d symbols=%d cols=%s datetime_profile=%s path=%s",
            label,
            table_name,
            rows,
            symbol_count,
            cols[:40],
            dt_info,
            db_path,
        )
    except Exception:
        logger.exception(
            "[ATS RANKING] table profile failed label=%s table=%s path=%s",
            label,
            table_name,
            db_path,
        )


def _read_table(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    db_path: Optional[str] = None,
) -> pd.DataFrame:
    if not _table_exists(conn, table_name):
        logger.info("[ATS RANKING] table missing table=%s path=%s", table_name, db_path)
        return pd.DataFrame()

    try:
        df = _safe_read_sql(conn, f"SELECT * FROM [{table_name}]")
        _log_table_profile(
            label="raw",
            table_name=table_name,
            df=df,
            db_path=db_path,
        )
        return df
    except Exception:
        logger.exception("[ATS RANKING] read table failed table=%s path=%s", table_name, db_path)
        return pd.DataFrame()


# ============================================================
# robust standardizer
# ============================================================

def _robust_standardize_snapshot_df(
    df: pd.DataFrame,
    table_name: str,
    *,
    db_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    normalizer._standardize_snapshot_df を優先し、
    それが空を返した場合に最低限の列補完で救済する。

    戻り値は ATS ranking builder が扱いやすい列を持つ DataFrame。
    """
    if df is None or df.empty:
        logger.warning(
            "[ATS RANKING] robust standardize skipped empty raw table=%s path=%s",
            table_name,
            db_path,
        )
        return pd.DataFrame()

    # --------------------------------------------------------
    # 1. 既存 normalizer を優先
    # --------------------------------------------------------
    try:
        std = _standardize_snapshot_df(df.copy(), table_name)
        if std is not None and not std.empty:
            _log_table_profile(
                label="standardized-by-normalizer",
                table_name=table_name,
                df=std,
                db_path=db_path,
            )
            return std
        logger.warning(
            "[ATS RANKING] normalizer returned empty table=%s raw_rows=%d path=%s -> trying robust fallback",
            table_name,
            len(df),
            db_path,
        )
    except Exception:
        logger.exception(
            "[ATS RANKING] normalizer failed table=%s raw_rows=%d path=%s -> trying robust fallback",
            table_name,
            len(df),
            db_path,
        )

    # --------------------------------------------------------
    # 2. fallback 標準化
    # --------------------------------------------------------
    try:
        out = df.copy()

        # 既存 common column 推論を先に使う
        try:
            out = _infer_common_columns(out)
        except Exception:
            logger.exception("[ATS RANKING] _infer_common_columns failed table=%s", table_name)

        # symbol
        try:
            out = _safe_symbol(out)
        except Exception:
            logger.exception("[ATS RANKING] _safe_symbol failed table=%s", table_name)
            out = pd.DataFrame()

        if out.empty:
            logger.warning(
                "[ATS RANKING] robust fallback failed because symbol normalization produced empty table=%s path=%s",
                table_name,
                db_path,
            )
            return pd.DataFrame()

        # symbolname
        out = _copy_series_if_exists(
            out,
            "symbolname",
            SYMBOLNAME_CANDIDATES,
            default="",
        )
        out["symbolname"] = out["symbolname"].fillna("").astype(str)

        # rank_type
        out = _copy_series_if_exists(
            out,
            "rank_type",
            RANK_TYPE_CANDIDATES,
            default=table_name,
        )
        out["rank_type"] = out["rank_type"].fillna("").astype(str)
        out.loc[out["rank_type"].str.strip() == "", "rank_type"] = table_name

        # ranking_type / category 互換
        if "ranking_type" not in out.columns:
            out["ranking_type"] = out["rank_type"]
        if "category" not in out.columns:
            out["category"] = out["rank_type"]

        # market / market_type
        if "market_type" not in out.columns:
            if "market" in out.columns:
                out["market_type"] = out["market"]
            elif "ExchangeName" in out.columns:
                out["market_type"] = out["ExchangeName"]
            else:
                out["market_type"] = ""

        if "market" not in out.columns:
            out["market"] = out["market_type"]

        out["market_type"] = out["market_type"].fillna("").astype(str).map(_normalize_market_type)
        out["market"] = out["market"].fillna("").astype(str)

        # rank_position
        out = _copy_series_if_exists(
            out,
            "rank_position",
            RANK_POSITION_CANDIDATES,
            default=np.nan,
        )
        out["rank_position"] = _safe_numeric_series(out["rank_position"], default=np.nan)
        if out["rank_position"].isna().all():
            out["rank_position"] = np.arange(1, len(out) + 1)

        # datetime / snapshot_time
        dt_col = _first_existing_col(out, DATETIME_CANDIDATES)
        if dt_col is not None:
            dts = _safe_dt_series(out[dt_col])
        else:
            dts = pd.Series(pd.Timestamp.now(), index=out.index)

        if dts.notna().sum() == 0:
            logger.warning(
                "[ATS RANKING] no valid datetime values table=%s dt_col=%s -> use now path=%s",
                table_name,
                dt_col,
                db_path,
            )
            dts = pd.Series(pd.Timestamp.now(), index=out.index)

        out["snapshot_time"] = dts
        out["datetime"] = dts

        # price
        out = _copy_series_if_exists(
            out,
            "current_price",
            PRICE_CANDIDATES,
            default=0,
        )
        out["current_price"] = _safe_numeric_series(out["current_price"], default=0)
        if "price" not in out.columns:
            out["price"] = out["current_price"]
        else:
            out["price"] = _safe_numeric_series(out["price"], default=0)
            mask = out["price"].fillna(0).eq(0) & out["current_price"].fillna(0).ne(0)
            out.loc[mask, "price"] = out.loc[mask, "current_price"]

        # volume
        out = _copy_series_if_exists(
            out,
            "trading_volume",
            VOLUME_CANDIDATES,
            default=0,
        )
        out["trading_volume"] = _safe_numeric_series(out["trading_volume"], default=0)
        if "volume" not in out.columns:
            out["volume"] = out["trading_volume"]
        else:
            out["volume"] = _safe_numeric_series(out["volume"], default=0)
            mask = out["volume"].fillna(0).eq(0) & out["trading_volume"].fillna(0).ne(0)
            out.loc[mask, "volume"] = out.loc[mask, "trading_volume"]

        # trading_value / turnover
        out = _copy_series_if_exists(
            out,
            "trading_value",
            TRADING_VALUE_CANDIDATES,
            default=0,
        )
        out["trading_value"] = _safe_numeric_series(out["trading_value"], default=0)
        if "turnover" not in out.columns:
            out["turnover"] = out["trading_value"]
        else:
            out["turnover"] = _safe_numeric_series(out["turnover"], default=0)
            mask = out["turnover"].fillna(0).eq(0) & out["trading_value"].fillna(0).ne(0)
            out.loc[mask, "turnover"] = out.loc[mask, "trading_value"]

        # numeric optional columns
        numeric_defaults = {
            "volume_speed": 0,
            "rank_strength": 0,
            "rank_persistence": 0,
            "rank_delta": 0,
            "price_delta_1m": 0,
            "volume_delta_1m": 0,
            "change_rate": 0,
            "change_percentage": 0,
            "tick_count": 0,
            "volume_spike": 0,
        }

        # change_rate aliases
        if "change_rate" not in out.columns:
            if "change_percentage" in out.columns:
                out["change_rate"] = out["change_percentage"]
            elif "ChangePercentage" in out.columns:
                out["change_rate"] = out["ChangePercentage"]
            elif "ChangeRatio" in out.columns:
                out["change_rate"] = out["ChangeRatio"]
            else:
                out["change_rate"] = 0

        if "change_percentage" not in out.columns:
            out["change_percentage"] = out["change_rate"]

        # tick_count aliases
        if "tick_count" not in out.columns:
            if "TickCount" in out.columns:
                out["tick_count"] = out["TickCount"]
            else:
                out["tick_count"] = 0

        for col, default in numeric_defaults.items():
            if col not in out.columns:
                out[col] = default
            out[col] = _safe_numeric_series(out[col], default=default)

        if "source" not in out.columns:
            out["source"] = table_name
        out["source"] = out["source"].fillna(table_name).astype(str)

        # minute_of_day
        if "minute_of_day" not in out.columns:
            try:
                out["minute_of_day"] = (
                    pd.to_datetime(out["snapshot_time"], errors="coerce").dt.hour * 60
                    + pd.to_datetime(out["snapshot_time"], errors="coerce").dt.minute
                )
            except Exception:
                out["minute_of_day"] = 0
        out["minute_of_day"] = _safe_numeric_series(out["minute_of_day"], default=0)

        # 最低限の有効行条件
        before = len(out)
        out = out[out["symbol"].astype(str).str.strip() != ""].copy()
        after_symbol = len(out)

        # price が 0 でもランキング候補としては残す。
        # ただしログで可視化する。
        price_positive = int((out["current_price"].fillna(0) > 0).sum()) if not out.empty else 0
        volume_positive = int((out["trading_volume"].fillna(0) > 0).sum()) if not out.empty else 0

        # 最新優先 dedup
        try:
            out["__dt__"] = pd.to_datetime(out["snapshot_time"], errors="coerce")
            out = out.sort_values("__dt__", ascending=False, kind="mergesort")
            out = out.drop_duplicates(subset=["symbol", "rank_type"], keep="first")
            out = out.drop(columns=["__dt__"], errors="ignore")
        except Exception:
            logger.exception("[ATS RANKING] robust fallback latest dedup failed table=%s", table_name)

        logger.info(
            "[ATS RANKING] robust standardized table=%s raw_rows=%d after_symbol=%d final_rows=%d symbols=%d price>0=%d volume>0=%d path=%s",
            table_name,
            before,
            after_symbol,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns and not out.empty else 0,
            price_positive,
            volume_positive,
            db_path,
        )

        _log_table_profile(
            label="standardized-by-robust-fallback",
            table_name=table_name,
            df=out,
            db_path=db_path,
        )

        return out

    except Exception:
        logger.exception(
            "[ATS RANKING] robust standardize failed table=%s path=%s",
            table_name,
            db_path,
        )
        return pd.DataFrame()


# ============================================================
# summary fallback
# ============================================================

def _get_summary_fallback() -> Optional[pd.DataFrame]:
    try:
        summary_cache = getattr(global_data, "summary_cache", {})
        if not isinstance(summary_cache, dict):
            logger.warning("[ATS RANKING] summary_cache is not dict type=%s", type(summary_cache).__name__)
            return None

        summary = summary_cache.get("1min")

        if summary is None:
            return None

        if isinstance(summary, pd.DataFrame) and summary.empty:
            return None

        try:
            df = summary.copy()
            logger.info(
                "[ATS RANKING] summary fallback source found rows=%d cols=%s",
                len(df) if isinstance(df, pd.DataFrame) else 0,
                list(df.columns)[:30] if isinstance(df, pd.DataFrame) else [],
            )
            return df
        except Exception:
            logger.exception("summary copy failed")
            return None

    except Exception:
        logger.exception("[ATS RANKING] summary fallback failed")
        return None


# ============================================================
# category table fallback
# ============================================================

def _read_category_symbols(
    conn: sqlite3.Connection,
    table_name: str,
    market_type: str,
    default_rank_type: str,
    *,
    db_path: Optional[str] = None,
) -> pd.DataFrame:
    if not _table_exists(conn, table_name):
        return pd.DataFrame()

    df = _safe_read_sql(conn, f"SELECT * FROM [{table_name}]")
    _log_table_profile(
        label="category-raw",
        table_name=table_name,
        df=df,
        db_path=db_path,
    )

    if df.empty:
        return df

    try:
        df = _infer_common_columns(df)
    except Exception:
        logger.exception("[ATS RANKING] category infer columns failed table=%s", table_name)

    try:
        df = _safe_symbol(df)
    except Exception:
        logger.exception("[ATS RANKING] category safe_symbol failed table=%s", table_name)
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    if "rank_type" not in df.columns:
        df["rank_type"] = default_rank_type
    else:
        df["rank_type"] = df["rank_type"].fillna("").astype(str).replace("", default_rank_type)

    if "ranking_type" not in df.columns:
        df["ranking_type"] = df["rank_type"]

    if "category" not in df.columns:
        df["category"] = df["rank_type"]

    if "market_type" not in df.columns:
        df["market_type"] = market_type
    else:
        df["market_type"] = df["market_type"].fillna("").astype(str).replace("", market_type)

    if "market" not in df.columns:
        df["market"] = df["market_type"]

    if "rank_position" not in df.columns:
        df["rank_position"] = np.arange(1, len(df) + 1)

    # datetime / snapshot_time
    dt_col = _first_existing_col(df, DATETIME_CANDIDATES)
    if dt_col is not None:
        dts = _safe_dt_series(df[dt_col])
    else:
        dts = pd.Series(pd.Timestamp.now(), index=df.index)

    if dts.notna().sum() == 0:
        dts = pd.Series(pd.Timestamp.now(), index=df.index)

    df["snapshot_time"] = dts
    df["datetime"] = dts

    if "symbolname" not in df.columns:
        name_col = _first_existing_col(df, SYMBOLNAME_CANDIDATES)
        if name_col is not None:
            df["symbolname"] = df[name_col]
        else:
            df["symbolname"] = ""

    if "source" not in df.columns:
        df["source"] = table_name

    # aliases
    df = _copy_series_if_exists(df, "current_price", PRICE_CANDIDATES, default=0)
    df = _copy_series_if_exists(df, "trading_volume", VOLUME_CANDIDATES, default=0)
    df = _copy_series_if_exists(df, "trading_value", TRADING_VALUE_CANDIDATES, default=0)

    if "price" not in df.columns:
        df["price"] = df["current_price"]
    if "volume" not in df.columns:
        df["volume"] = df["trading_volume"]
    if "turnover" not in df.columns:
        df["turnover"] = df["trading_value"]

    if "change_rate" not in df.columns:
        if "change_percentage" in df.columns:
            df["change_rate"] = df["change_percentage"]
        elif "ChangePercentage" in df.columns:
            df["change_rate"] = df["ChangePercentage"]
        else:
            df["change_rate"] = 0

    if "change_percentage" not in df.columns:
        df["change_percentage"] = df["change_rate"]

    if "tick_count" not in df.columns:
        if "TickCount" in df.columns:
            df["tick_count"] = df["TickCount"]
        else:
            df["tick_count"] = 0

    for col in [
        "current_price",
        "price",
        "trading_volume",
        "volume",
        "trading_value",
        "turnover",
        "volume_speed",
        "rank_strength",
        "rank_persistence",
        "rank_delta",
        "price_delta_1m",
        "volume_delta_1m",
        "change_rate",
        "change_percentage",
        "tick_count",
        "volume_spike",
    ]:
        if col not in df.columns:
            df[col] = 0
        df[col] = _safe_numeric_series(df[col], default=0)

    df["market_type"] = df["market_type"].map(_normalize_market_type)
    df["rank_position"] = _safe_numeric_series(df["rank_position"], default=np.nan)

    try:
        tmp = df.copy()
        tmp["__dt__"] = pd.to_datetime(tmp["snapshot_time"], errors="coerce")
        tmp = tmp.sort_values("__dt__", ascending=False, kind="mergesort")
        tmp = tmp.drop_duplicates(subset=["symbol", "rank_type"], keep="first")
        df = tmp.drop(columns=["__dt__"], errors="ignore")
    except Exception:
        logger.exception("category latest dedup failed")

    _log_table_profile(
        label="category-standardized",
        table_name=table_name,
        df=df,
        db_path=db_path,
    )

    return df


def _build_fallback_from_category_tables(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    frames = []

    for table_name, market_type, default_rank_type in CATEGORY_TABLE_SPECS:
        part = _read_category_symbols(
            conn,
            table_name,
            market_type,
            default_rank_type,
            db_path=db_path,
        )
        if part is not None and not part.empty:
            frames.append(part)

    if not frames:
        return None

    merged = pd.concat(frames, ignore_index=True, sort=False)

    try:
        merged = _safe_symbol(merged)
    except Exception:
        logger.exception("[ATS RANKING] category merged safe_symbol failed")
        return None

    if merged.empty:
        return None

    try:
        merged["__dt__"] = pd.to_datetime(merged["snapshot_time"], errors="coerce")
        merged = merged.sort_values("__dt__", ascending=False, kind="mergesort")
        merged = merged.drop_duplicates(subset=["symbol", "rank_type"], keep="first")
        merged = merged.drop(columns=["__dt__"], errors="ignore")
    except Exception:
        logger.exception("[ATS RANKING] category merged latest dedup failed")

    logger.info(
        "[ATS RANKING] using category tables rows=%d symbols=%d path=%s",
        len(merged),
        merged["symbol"].nunique() if "symbol" in merged.columns else 0,
        db_path,
    )

    _log_table_profile(
        label="category-merged",
        table_name="CATEGORY_TABLES",
        df=merged,
        db_path=db_path,
    )

    return merged


# ============================================================
# primary table loader
# ============================================================

def _load_primary_table(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    db_path: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    df = _read_table(conn, table_name, db_path=db_path)
    if df is None or df.empty:
        logger.warning(
            "[ATS RANKING] primary table empty table=%s path=%s",
            table_name,
            db_path,
        )
        return None

    std = _robust_standardize_snapshot_df(
        df,
        table_name,
        db_path=db_path,
    )

    if std is not None and not std.empty:
        logger.info(
            "[ATS RANKING] using primary table: %s rows=%d symbols=%d path=%s",
            table_name,
            len(std),
            std["symbol"].nunique() if "symbol" in std.columns else 0,
            db_path,
        )
        return std

    logger.warning(
        "[ATS RANKING] primary table unusable after standardize table=%s raw_rows=%d path=%s",
        table_name,
        len(df),
        db_path,
    )
    return None


def _load_from_ranking_db() -> Optional[pd.DataFrame]:
    db_path = get_usable_ranking_db_path(force_refresh=False)

    if not db_path:
        logger.warning("[ATS RANKING] ranking DB path unresolved")
        return None

    logger.info("[ATS RANKING] ranking DB path=%s", db_path)

    try:
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=5000")
            except Exception:
                pass

            tables = set(_list_tables(conn))
            logger.info(
                "[ATS RANKING] db tables path=%s tables=%s",
                db_path,
                sorted(tables),
            )

            # ------------------------------------------------
            # 1. primary tables
            # ------------------------------------------------
            for table_name in PRIMARY_TABLES_ORDER:
                if table_name not in tables:
                    logger.info(
                        "[ATS RANKING] primary table not found table=%s path=%s",
                        table_name,
                        db_path,
                    )
                    continue

                std = _load_primary_table(conn, table_name, db_path=db_path)
                if std is not None and not std.empty:
                    return std

            # ------------------------------------------------
            # 2. category fallback
            # ------------------------------------------------
            df = _build_fallback_from_category_tables(conn, db_path=db_path)
            if df is not None and not df.empty:
                logger.info(
                    "[ATS RANKING] using category fallback rows=%d symbols=%d path=%s",
                    len(df),
                    df["symbol"].nunique() if "symbol" in df.columns else 0,
                    db_path,
                )
                return df

            logger.warning(
                "[ATS RANKING] no usable ranking tables found path=%s tables=%s",
                db_path,
                sorted(tables),
            )
            return None

    except Exception:
        logger.exception("[ATS RANKING] ranking DB load failed path=%s", db_path)
        return None


# ============================================================
# public base source
# ============================================================

def _get_base_source_df() -> Optional[pd.DataFrame]:
    """
    ATS ranking builder が利用する基礎 DataFrame を返す。

    優先順位:
      1. 短期 cache
      2. ranking DB
      3. summary_cache fallback

    注意:
      cache が古いDB由来の可能性を完全に排除するには、
      db_path 側の cache invalidation と併用する。
    """
    from . import cache
    import time

    now = time.time()

    if (
        cache._ATS_RANKING_CACHE_DF is not None
        and (now - cache._ATS_RANKING_CACHE_TS) < cache._ATS_RANKING_CACHE_SEC
    ):
        try:
            cached = cache._ATS_RANKING_CACHE_DF.copy()
            logger.info(
                "[ATS RANKING] return cached base df rows=%d symbols=%d age=%.3fs",
                len(cached),
                cached["symbol"].nunique() if isinstance(cached, pd.DataFrame) and "symbol" in cached.columns else 0,
                now - cache._ATS_RANKING_CACHE_TS,
            )
            return cached
        except Exception:
            logger.exception("ATS ranking cache copy failed")

    df = _load_from_ranking_db()

    if df is not None and not df.empty:
        try:
            cache._ATS_RANKING_CACHE_DF = df.copy()
            cache._ATS_RANKING_CACHE_TS = now
        except Exception:
            logger.exception("ATS ranking cache store failed")
        return df

    df = _get_summary_fallback()

    if df is not None and not df.empty:
        try:
            std = _standardize_summary_fallback(df)
            if std is not None and not std.empty:
                try:
                    cache._ATS_RANKING_CACHE_DF = std.copy()
                    cache._ATS_RANKING_CACHE_TS = now
                except Exception:
                    logger.exception("ATS ranking summary cache store failed")

                logger.info(
                    "[ATS RANKING] using summary fallback rows=%d symbols=%d",
                    len(std),
                    std["symbol"].nunique() if "symbol" in std.columns else 0,
                )
                return std

            logger.warning(
                "[ATS RANKING] summary fallback standardize returned empty raw_rows=%d",
                len(df),
            )

        except Exception:
            logger.exception("[ATS RANKING] summary fallback standardize failed")

    logger.warning("[ATS RANKING] base source df unavailable")
    return None


__all__ = [
    "_get_base_source_df",
]