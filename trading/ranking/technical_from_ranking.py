# ============================================================
# File   : trading/ranking/summary/technical_from_ranking.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-TECH-FROM-PRICE
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリー専用のテクニカル計算モジュール
#
# 【重要方針】
#   - PUSH由来 summary / stock_summary_* は一切読まない
#   - ranking_snapshot_1min.current_price を close として扱う
#   - Yahoo 1分足 close はランキング価格系列の欠損補完にのみ使う
#   - 出力はランキング由来専用 DataFrame
#
# 【主な機能】
#   - ranking_snapshot_1min から current_price 履歴を取得
#   - Yahoo 1min DB から close 補完値を取得
#   - symbol + datetime 単位で価格系列を構築
#   - RSI / MACD / MACD signal / MACD hist を計算
#   - 1min / 3min / 5min に対応
#
# 【出力主要列】
#   symbol, symbolname, datetime, close, current_price,
#   rsi, macd, macd_signal, macd_hist, price_source
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Default paths
# ============================================================

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"
DEFAULT_RANKING_DIR = os.path.join(
    DEFAULT_BASE_DIR,
    "raw_data",
    "kabu_station",
    "ranking",
)
DEFAULT_YAHOO_DIR = os.path.join(
    DEFAULT_BASE_DIR,
    "raw_data",
    "yahoo",
    "intraday",
)


# ============================================================
# Utilities
# ============================================================

def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _normalize_yyyymmdd(value: Optional[str | int | dt.date | dt.datetime]) -> str:
    if value is None:
        return _today_yyyymmdd()

    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")

    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")

    s = str(value).strip()
    if not s:
        return _today_yyyymmdd()

    if "-" in s:
        return pd.to_datetime(s).strftime("%Y%m%d")

    if len(s) == 8 and s.isdigit():
        return s

    return pd.to_datetime(s).strftime("%Y%m%d")


def _ranking_db_path(
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    *,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> str:
    ymd = _normalize_yyyymmdd(trade_date)
    return os.path.join(ranking_dir, f"ranking{ymd}.db")


def _yahoo_db_path(
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    *,
    yahoo_dir: str = DEFAULT_YAHOO_DIR,
) -> str:
    ymd = _normalize_yyyymmdd(trade_date)
    return os.path.join(yahoo_dir, f"yahoo_1min_{ymd}.db")


def _connect_readonly(path: str) -> sqlite3.Connection:
    """
    SQLiteを読み取り専用で開く。
    Windows UNCパスでも通常接続にフォールバックする。
    """
    if not path:
        raise ValueError("empty sqlite path")

    if not os.path.exists(path):
        raise FileNotFoundError(path)

    try:
        uri = f"file:{Path(path).as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
    except Exception:
        con = sqlite3.connect(path, timeout=10)

    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass

    return con


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    try:
        row = con.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type='table'
               AND name=?
             LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _safe_to_datetime(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.tz_localize(None)


def _normalize_symbol_series(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )


def _as_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _floor_interval_datetime(series: pd.Series, interval: int) -> pd.Series:
    """
    00分起点で interval 分に丸める。
    1 -> そのまま minute floor
    3 -> 09:00,09:03,09:06...
    5 -> 09:00,09:05,09:10...
    """
    dt_series = _safe_to_datetime(series)
    if interval <= 1:
        return dt_series.dt.floor("min")

    floored = dt_series.dt.floor("min")
    minute = floored.dt.minute
    base_minute = (minute // interval) * interval

    return (
        floored
        - pd.to_timedelta(minute - base_minute, unit="m")
        - pd.to_timedelta(floored.dt.second, unit="s")
        - pd.to_timedelta(floored.dt.microsecond, unit="us")
    )


# ============================================================
# Load ranking snapshot
# ============================================================

def load_ranking_snapshot_price_history(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
    lookback_minutes: int = 240,
    symbols: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """
    ranking_snapshot_1min から current_price 履歴を取得する。

    Returns
    -------
    DataFrame:
        symbol, symbolname, datetime, close, current_price,
        ranking_type, rank, change_percentage, trading_volume,
        trading_value, tick_count, price_source
    """
    path = db_path or _ranking_db_path(trade_date, ranking_dir=ranking_dir)

    if not os.path.exists(path):
        logger.warning("[RANKING TECH] ranking db not found path=%s", path)
        return pd.DataFrame()

    since_dt = dt.datetime.now() - dt.timedelta(minutes=int(lookback_minutes))

    symbol_list = None
    if symbols is not None:
        symbol_list = [str(x).strip() for x in symbols if str(x).strip()]
        if not symbol_list:
            symbol_list = None

    try:
        with _connect_readonly(path) as con:
            if not _table_exists(con, "ranking_snapshot_1min"):
                logger.warning(
                    "[RANKING TECH] table not found ranking_snapshot_1min path=%s",
                    path,
                )
                return pd.DataFrame()

            cols = pd.read_sql_query(
                "PRAGMA table_info(ranking_snapshot_1min)",
                con,
            )["name"].tolist()

            datetime_col = None
            for c in ("datetime", "inserted_at", "created_at", "timestamp"):
                if c in cols:
                    datetime_col = c
                    break

            if datetime_col is None:
                logger.warning(
                    "[RANKING TECH] datetime column not found cols=%s",
                    cols,
                )
                return pd.DataFrame()

            select_cols = []
            for c in [
                "symbol",
                "symbolname",
                datetime_col,
                "current_price",
                "ranking_type",
                "type",
                "rank",
                "change_percentage",
                "trading_volume",
                "trading_value",
                "turnover",
                "tick_count",
            ]:
                if c in cols and c not in select_cols:
                    select_cols.append(c)

            if "symbol" not in select_cols or "current_price" not in select_cols:
                logger.warning(
                    "[RANKING TECH] required columns missing cols=%s",
                    cols,
                )
                return pd.DataFrame()

            sql = f"""
                SELECT {", ".join(select_cols)}
                  FROM ranking_snapshot_1min
                 WHERE {datetime_col} >= ?
            """
            params: list[object] = [since_dt.strftime("%Y-%m-%d %H:%M:%S")]

            if symbol_list:
                ph = ",".join(["?"] * len(symbol_list))
                sql += f" AND symbol IN ({ph})"
                params.extend(symbol_list)

            df = pd.read_sql_query(sql, con, params=params)

    except Exception:
        logger.exception("[RANKING TECH] load ranking snapshot failed path=%s", path)
        return pd.DataFrame()

    if df.empty:
        logger.info("[RANKING TECH] ranking snapshot empty path=%s", path)
        return df

    df = df.rename(columns={datetime_col: "datetime"})
    if "type" in df.columns and "ranking_type" not in df.columns:
        df = df.rename(columns={"type": "ranking_type"})

    df["symbol"] = _normalize_symbol_series(df["symbol"])
    df["datetime"] = _safe_to_datetime(df["datetime"])
    df["current_price"] = _as_numeric(df["current_price"])
    df["close"] = df["current_price"]
    df["price_source"] = "ranking"

    if "symbolname" not in df.columns:
        df["symbolname"] = ""

    if "ranking_type" not in df.columns:
        df["ranking_type"] = ""

    for c in [
        "rank",
        "change_percentage",
        "trading_volume",
        "trading_value",
        "turnover",
        "tick_count",
    ]:
        if c not in df.columns:
            df[c] = np.nan
        else:
            df[c] = _as_numeric(df[c])

    df = df.dropna(subset=["symbol", "datetime", "close"])
    df = df[df["symbol"] != ""]
    df = df.sort_values(["symbol", "datetime"])

    # 同一 symbol/datetime に複数 ranking_type がある場合は最後を採用。
    # ranking_type別の分析は別途保持可能だが、テクニカル計算は価格系列なので1点に寄せる。
    df = df.drop_duplicates(["symbol", "datetime"], keep="last")

    logger.info(
        "[RANKING TECH] loaded ranking prices rows=%s symbols=%s dt_min=%s dt_max=%s",
        len(df),
        df["symbol"].nunique(),
        df["datetime"].min() if not df.empty else None,
        df["datetime"].max() if not df.empty else None,
    )

    return df.reset_index(drop=True)


# ============================================================
# Load Yahoo 1min
# ============================================================

def load_yahoo_1min_close_history(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    db_path: Optional[str] = None,
    yahoo_dir: str = DEFAULT_YAHOO_DIR,
    lookback_minutes: int = 240,
    symbols: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """
    Yahoo 1分足 DB から close 履歴を読む。

    想定テーブル名は環境差を吸収するため候補探索する。
    """
    path = db_path or _yahoo_db_path(trade_date, yahoo_dir=yahoo_dir)

    if not os.path.exists(path):
        logger.warning("[RANKING TECH] yahoo db not found path=%s", path)
        return pd.DataFrame()

    symbol_list = None
    if symbols is not None:
        symbol_list = [str(x).strip() for x in symbols if str(x).strip()]
        if not symbol_list:
            symbol_list = None

    since_dt = dt.datetime.now() - dt.timedelta(minutes=int(lookback_minutes))

    table_candidates = [
        "yahoo_1min",
        "intraday_1min",
        "stock_1min",
        "price_1min",
        "ohlcv_1min",
    ]

    try:
        with _connect_readonly(path) as con:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'",
                con,
            )["name"].tolist()

            table_name = None
            for t in table_candidates:
                if t in tables:
                    table_name = t
                    break

            if table_name is None:
                # 候補外でも symbol/datetime/close を持つテーブルを探す
                for t in tables:
                    cols = pd.read_sql_query(f"PRAGMA table_info({t})", con)["name"].tolist()
                    if "symbol" in cols and "close" in cols and any(
                        c in cols for c in ("datetime", "timestamp", "date")
                    ):
                        table_name = t
                        break

            if table_name is None:
                logger.warning(
                    "[RANKING TECH] yahoo price table not found path=%s tables=%s",
                    path,
                    tables,
                )
                return pd.DataFrame()

            cols = pd.read_sql_query(f"PRAGMA table_info({table_name})", con)["name"].tolist()

            datetime_col = None
            for c in ("datetime", "timestamp", "date"):
                if c in cols:
                    datetime_col = c
                    break

            if datetime_col is None or "symbol" not in cols or "close" not in cols:
                logger.warning(
                    "[RANKING TECH] yahoo required cols missing table=%s cols=%s",
                    table_name,
                    cols,
                )
                return pd.DataFrame()

            select_cols = ["symbol", datetime_col, "close"]
            for c in ["open", "high", "low", "volume"]:
                if c in cols:
                    select_cols.append(c)

            sql = f"""
                SELECT {", ".join(select_cols)}
                  FROM {table_name}
                 WHERE {datetime_col} >= ?
            """
            params: list[object] = [since_dt.strftime("%Y-%m-%d %H:%M:%S")]

            if symbol_list:
                ph = ",".join(["?"] * len(symbol_list))
                sql += f" AND symbol IN ({ph})"
                params.extend(symbol_list)

            df = pd.read_sql_query(sql, con, params=params)

    except Exception:
        logger.exception("[RANKING TECH] load yahoo failed path=%s", path)
        return pd.DataFrame()

    if df.empty:
        logger.info("[RANKING TECH] yahoo history empty path=%s", path)
        return df

    df = df.rename(columns={datetime_col: "datetime"})
    df["symbol"] = _normalize_symbol_series(df["symbol"])
    df["datetime"] = _safe_to_datetime(df["datetime"])
    df["close"] = _as_numeric(df["close"])

    for c in ["open", "high", "low", "volume"]:
        if c in df.columns:
            df[c] = _as_numeric(df[c])

    df = df.dropna(subset=["symbol", "datetime", "close"])
    df = df[df["symbol"] != ""]
    df = df.sort_values(["symbol", "datetime"])
    df = df.drop_duplicates(["symbol", "datetime"], keep="last")
    df["price_source"] = "yahoo_fill"

    logger.info(
        "[RANKING TECH] loaded yahoo prices rows=%s symbols=%s dt_min=%s dt_max=%s",
        len(df),
        df["symbol"].nunique(),
        df["datetime"].min() if not df.empty else None,
        df["datetime"].max() if not df.empty else None,
    )

    return df.reset_index(drop=True)


# ============================================================
# Price series composition
# ============================================================

def build_ranking_price_series(
    ranking_df: pd.DataFrame,
    yahoo_df: Optional[pd.DataFrame] = None,
    *,
    interval: int = 1,
) -> pd.DataFrame:
    """
    ランキング由来の価格系列を構築する。

    優先順位:
      1. ranking current_price
      2. yahoo close

    PUSH summary とは一切 merge しない。
    """
    if ranking_df is None or ranking_df.empty:
        logger.warning("[RANKING TECH] ranking_df empty; cannot build series")
        return pd.DataFrame()

    interval = int(interval or 1)
    if interval not in (1, 3, 5):
        logger.warning("[RANKING TECH] unsupported interval=%s -> fallback 1", interval)
        interval = 1

    r = ranking_df.copy()
    r["symbol"] = _normalize_symbol_series(r["symbol"])
    r["datetime"] = _floor_interval_datetime(r["datetime"], interval)
    r["close"] = _as_numeric(r.get("close", r.get("current_price")))

    if "current_price" not in r.columns:
        r["current_price"] = r["close"]

    if "price_source" not in r.columns:
        r["price_source"] = "ranking"

    if "symbolname" not in r.columns:
        r["symbolname"] = ""

    if "ranking_type" not in r.columns:
        r["ranking_type"] = ""

    for c in [
        "rank",
        "change_percentage",
        "trading_volume",
        "trading_value",
        "turnover",
        "tick_count",
    ]:
        if c not in r.columns:
            r[c] = np.nan

    r = r.dropna(subset=["symbol", "datetime", "close"])
    r = r.sort_values(["symbol", "datetime"])

    # interval集約時は最後の価格を採用
    agg = {
        "symbolname": "last",
        "close": "last",
        "current_price": "last",
        "ranking_type": "last",
        "rank": "last",
        "change_percentage": "last",
        "trading_volume": "last",
        "trading_value": "last",
        "turnover": "last",
        "tick_count": "last",
        "price_source": "last",
    }
    r = (
        r.groupby(["symbol", "datetime"], as_index=False)
         .agg(agg)
         .sort_values(["symbol", "datetime"])
    )

    if yahoo_df is None or yahoo_df.empty:
        return r.reset_index(drop=True)

    y = yahoo_df.copy()
    y["symbol"] = _normalize_symbol_series(y["symbol"])
    y["datetime"] = _floor_interval_datetime(y["datetime"], interval)
    y["close"] = _as_numeric(y["close"])
    y = y.dropna(subset=["symbol", "datetime", "close"])
    y = y[y["symbol"].isin(set(r["symbol"].unique()))]

    if y.empty:
        return r.reset_index(drop=True)

    y = (
        y.sort_values(["symbol", "datetime"])
         .groupby(["symbol", "datetime"], as_index=False)
         .agg({"close": "last"})
    )
    y["price_source"] = "yahoo_fill"

    # rankingに存在しない symbol/datetime のみ Yahoo を追加
    key_cols = ["symbol", "datetime"]
    r_keys = r[key_cols].drop_duplicates()
    y_only = y.merge(r_keys, on=key_cols, how="left", indicator=True)
    y_only = y_only[y_only["_merge"] == "left_only"].drop(columns=["_merge"])

    if y_only.empty:
        return r.reset_index(drop=True)

    # ranking側の属性を symbol ごとに補完
    latest_meta = (
        r.sort_values(["symbol", "datetime"])
         .groupby("symbol", as_index=False)
         .tail(1)[["symbol", "symbolname", "ranking_type"]]
    )
    y_only = y_only.merge(latest_meta, on="symbol", how="left")
    y_only["current_price"] = y_only["close"]

    for c in [
        "rank",
        "change_percentage",
        "trading_volume",
        "trading_value",
        "turnover",
        "tick_count",
    ]:
        y_only[c] = np.nan

    out = pd.concat([r, y_only[r.columns]], ignore_index=True)
    out = out.sort_values(["symbol", "datetime"])
    out = out.drop_duplicates(["symbol", "datetime"], keep="first")

    logger.info(
        "[RANKING TECH] built price series interval=%s rows=%s symbols=%s "
        "ranking_rows=%s yahoo_fill_rows=%s",
        interval,
        len(out),
        out["symbol"].nunique(),
        len(r),
        len(y_only),
    )

    return out.reset_index(drop=True)


# ============================================================
# Indicators
# ============================================================

def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    close = pd.to_numeric(close, errors="coerce")
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder風のEMA
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # loss=0 で gain>0 の場合は100寄り
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)

    return rsi


def _calc_macd(
    close: pd.Series,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = pd.to_numeric(close, errors="coerce")

    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()

    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=signal, adjust=False, min_periods=signal).mean()
    macd_hist = macd - macd_signal

    return macd, macd_signal, macd_hist


def add_ranking_indicators(
    price_df: pd.DataFrame,
    *,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> pd.DataFrame:
    """
    ranking current_price / yahoo_fill close 系列から
    RSI / MACD を計算する。
    """
    if price_df is None or price_df.empty:
        return pd.DataFrame()

    df = price_df.copy()
    df["symbol"] = _normalize_symbol_series(df["symbol"])
    df["datetime"] = _safe_to_datetime(df["datetime"])
    df["close"] = _as_numeric(df["close"])
    df = df.dropna(subset=["symbol", "datetime", "close"])
    df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    pieces: list[pd.DataFrame] = []

    for symbol, g in df.groupby("symbol", sort=False):
        one = g.copy().sort_values("datetime")
        close = one["close"]

        one["rsi"] = _calc_rsi(close, period=rsi_period)

        macd, sig, hist = _calc_macd(
            close,
            fast=macd_fast,
            slow=macd_slow,
            signal=macd_signal,
        )
        one["macd"] = macd
        one["macd_signal"] = sig
        one["macd_hist"] = hist

        one["rsi_slope"] = one["rsi"].diff()
        one["macd_hist_slope"] = one["macd_hist"].diff()

        pieces.append(one)

    if not pieces:
        return pd.DataFrame()

    out = pd.concat(pieces, ignore_index=True)
    out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    logger.info(
        "[RANKING TECH] indicators added rows=%s symbols=%s "
        "rsi_non_null=%s macd_non_null=%s hist_non_null=%s",
        len(out),
        out["symbol"].nunique(),
        int(out["rsi"].notna().sum()) if "rsi" in out.columns else 0,
        int(out["macd"].notna().sum()) if "macd" in out.columns else 0,
        int(out["macd_hist"].notna().sum()) if "macd_hist" in out.columns else 0,
    )

    return out


# ============================================================
# Simple ranking-specific flags
# ============================================================

def add_ranking_indicator_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    score_config.ini の flag 名に合わせて、
    ランキング由来の最低限の MACD / RSI flag を立てる。

    本格的なローソク足パターンや板情報系 flag は、
    ランキング価格系列だけでは判定できないためここでは作らない。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = out.sort_values(["symbol", "datetime"])

    for c in [
        "flag_macd_cross",
        "flag_macd_hist_expand",
        "flag_rsi_rebound",
        "flag_rsi_midline_cross",
        "flag_macd_dc",
        "flag_macd_hist_contract",
        "flag_rsi_falling",
        "flag_rsi_overbought_70",
    ]:
        if c not in out.columns:
            out[c] = False

    pieces: list[pd.DataFrame] = []

    for symbol, g in out.groupby("symbol", sort=False):
        one = g.copy().sort_values("datetime")

        prev_macd = one["macd"].shift(1)
        prev_sig = one["macd_signal"].shift(1)
        prev_hist = one["macd_hist"].shift(1)
        prev_rsi = one["rsi"].shift(1)

        one["flag_macd_cross"] = (
            (prev_macd <= prev_sig)
            & (one["macd"] > one["macd_signal"])
        )

        one["flag_macd_dc"] = (
            (prev_macd >= prev_sig)
            & (one["macd"] < one["macd_signal"])
        )

        one["flag_macd_hist_expand"] = (
            (one["macd_hist"] > 0)
            & (one["macd_hist"] > prev_hist)
        )

        one["flag_macd_hist_contract"] = (
            (one["macd_hist"] > 0)
            & (one["macd_hist"] < prev_hist)
        )

        one["flag_rsi_rebound"] = (
            (prev_rsi < 40)
            & (one["rsi"] >= 40)
        )

        one["flag_rsi_midline_cross"] = (
            (prev_rsi <= 50)
            & (one["rsi"] > 50)
        )

        one["flag_rsi_falling"] = one["rsi"] < prev_rsi

        one["flag_rsi_overbought_70"] = one["rsi"] >= 70

        pieces.append(one)

    out = pd.concat(pieces, ignore_index=True)
    return out.sort_values(["symbol", "datetime"]).reset_index(drop=True)


# ============================================================
# Simple score fallback
# ============================================================

def add_ranking_simple_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    scoring_pipeline が未接続でもランキング由来TOP10を動かすための
    最低限のフォールバックスコア。

    既存の scoring_pipeline を使う場合でも、
    score が未生成ならここで補完する。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    score = pd.Series(0.0, index=out.index)

    weights = {
        "flag_macd_cross": 3.0,
        "flag_macd_hist_expand": 1.0,
        "flag_rsi_rebound": 1.0,
        "flag_rsi_midline_cross": 2.0,
        "flag_macd_dc": -3.0,
        "flag_macd_hist_contract": -1.0,
        "flag_rsi_falling": -1.0,
        "flag_rsi_overbought_70": -2.0,
    }

    for c, w in weights.items():
        if c in out.columns:
            score += out[c].fillna(False).astype(bool).astype(float) * w

    # ランキング瞬間値も軽く加味
    if "change_percentage" in out.columns:
        cp = pd.to_numeric(out["change_percentage"], errors="coerce").fillna(0)
        score += np.clip(cp, -5, 5) * 0.2

    if "trading_value" in out.columns:
        tv = pd.to_numeric(out["trading_value"], errors="coerce")
        tv_rank = tv.rank(pct=True)
        score += tv_rank.fillna(0) * 1.0

    if "tick_count" in out.columns:
        tc = pd.to_numeric(out["tick_count"], errors="coerce")
        tc_rank = tc.rank(pct=True)
        score += tc_rank.fillna(0) * 1.0

    if "score" not in out.columns or out["score"].isna().all():
        out["score"] = score
    else:
        out["score"] = pd.to_numeric(out["score"], errors="coerce").fillna(score)

    if "final_score" not in out.columns:
        out["final_score"] = out["score"]

    if "display_score" not in out.columns:
        out["display_score"] = out["final_score"]

    return out


# ============================================================
# Public build API
# ============================================================

def build_ranking_summary_technical(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    interval: int = 1,
    lookback_minutes: int = 240,
    symbols: Optional[Iterable[str]] = None,
    ranking_db_path: Optional[str] = None,
    yahoo_db_path: Optional[str] = None,
    use_yahoo_fill: bool = True,
) -> pd.DataFrame:
    """
    ランキング由来サマリーを作るメイン関数。

    Parameters
    ----------
    interval:
        1, 3, 5 のみ対応。
    use_yahoo_fill:
        True の場合、ランキング価格がない時刻を Yahoo close で補完する。

    Returns
    -------
    DataFrame:
        ranking_summary_* に保存可能な DataFrame。
    """
    interval = int(interval or 1)
    if interval not in (1, 3, 5):
        raise ValueError(f"unsupported interval: {interval}")

    ranking_df = load_ranking_snapshot_price_history(
        trade_date=trade_date,
        db_path=ranking_db_path,
        lookback_minutes=lookback_minutes,
        symbols=symbols,
    )

    if ranking_df.empty:
        logger.warning("[RANKING TECH] no ranking source rows interval=%s", interval)
        return pd.DataFrame()

    yahoo_df = pd.DataFrame()
    if use_yahoo_fill:
        yahoo_df = load_yahoo_1min_close_history(
            trade_date=trade_date,
            db_path=yahoo_db_path,
            lookback_minutes=lookback_minutes,
            symbols=ranking_df["symbol"].unique(),
        )

    price_df = build_ranking_price_series(
        ranking_df,
        yahoo_df,
        interval=interval,
    )

    if price_df.empty:
        logger.warning("[RANKING TECH] price series empty interval=%s", interval)
        return pd.DataFrame()

    tech_df = add_ranking_indicators(price_df)
    tech_df = add_ranking_indicator_flags(tech_df)
    tech_df = add_ranking_simple_score(tech_df)

    tech_df["interval"] = int(interval)
    tech_df["source"] = "ranking"

    logger.info(
        "[RANKING TECH] built ranking summary interval=%s rows=%s symbols=%s "
        "dt_min=%s dt_max=%s",
        interval,
        len(tech_df),
        tech_df["symbol"].nunique() if "symbol" in tech_df.columns else 0,
        tech_df["datetime"].min() if "datetime" in tech_df.columns and not tech_df.empty else None,
        tech_df["datetime"].max() if "datetime" in tech_df.columns and not tech_df.empty else None,
    )

    return tech_df.reset_index(drop=True)


def get_latest_ranking_summary_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    各symbolの最新行だけを返す。
    TOP10表示用。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out["datetime"] = _safe_to_datetime(out["datetime"])
    out = out.dropna(subset=["symbol", "datetime"])
    out = out.sort_values(["symbol", "datetime"])
    out = out.groupby("symbol", as_index=False).tail(1)
    return out.sort_values("display_score", ascending=False).reset_index(drop=True)