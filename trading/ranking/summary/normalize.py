# ============================================================
# File   : trading/ranking/summary/normalize.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-NORMALIZE
# ------------------------------------------------------------
# 【概要】
#   ranking_snapshot_1min の列名・型をランキングサマリー用に正規化する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Optional

import pandas as pd

from trading.ranking.summary.constants import (
    CHANGE_CANDIDATES,
    DATETIME_CANDIDATES,
    OPTIONAL_NUMERIC_COLS,
    PRICE_CANDIDATES,
    RANK_CANDIDATES,
    TYPE_CANDIDATES,
    TURNOVER_CANDIDATES,
    VOLUME_CANDIDATES,
)
from trading.ranking.summary.utils import (
    combine_numeric_columns,
    combine_text_columns,
    first_existing_col,
    log_df_profile,
    normalize_symbols,
)

logger = logging.getLogger(__name__)


def normalize_ranking_snapshot_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking_snapshot_1min を summary 作成用の標準列へ整える。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    x = x.loc[:, ~pd.Index(x.columns).duplicated()].copy()

    log_df_profile("raw-before-normalize", x)

    # --------------------------------------------------------
    # datetime
    # --------------------------------------------------------
    dt_col = first_existing_col(x, DATETIME_CANDIDATES)

    if dt_col is None:
        logger.warning(
            "[RANKING SUMMARY RUNNER] no datetime column cols=%s",
            list(x.columns),
        )
        return pd.DataFrame()

    x["datetime"] = pd.to_datetime(x[dt_col], errors="coerce")

    try:
        x["datetime"] = x["datetime"].dt.tz_localize(None)
    except Exception:
        pass

    before = len(x)
    x = x[x["datetime"].notna()].copy()

    logger.info(
        "[RANKING SUMMARY RUNNER] datetime normalized dt_col=%s rows=%s->%s",
        dt_col,
        before,
        len(x),
    )

    if x.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # symbol
    # --------------------------------------------------------
    if "symbol" not in x.columns:
        logger.warning("[RANKING SUMMARY RUNNER] no symbol column")
        return pd.DataFrame()

    before = len(x)
    x["symbol"] = x["symbol"].astype(str).str.strip()
    x = x[
        x["symbol"].ne("")
        & ~x["symbol"].str.upper().str.startswith("FILLER_")
    ].copy()

    logger.info(
        "[RANKING SUMMARY RUNNER] symbol normalized rows=%s->%s symbols=%s",
        before,
        len(x),
        x["symbol"].nunique() if not x.empty else 0,
    )

    if x.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # symbolname
    # --------------------------------------------------------
    if "symbolname" not in x.columns:
        x["symbolname"] = ""

    try:
        x["symbolname"] = x["symbolname"].fillna("").astype(str).str.strip()
    except Exception:
        x["symbolname"] = ""

    # --------------------------------------------------------
    # price / close
    # --------------------------------------------------------
    x["close"] = combine_numeric_columns(x, PRICE_CANDIDATES)
    x["current_price"] = x["close"]

    try:
        x = x.sort_values(["symbol", "datetime"], kind="mergesort")
        x["close"] = x.groupby("symbol", sort=False)["close"].ffill().bfill()
        x["current_price"] = x["close"]
    except Exception:
        logger.exception("[RANKING SUMMARY RUNNER] close ffill/bfill failed")

    before = len(x)
    close_num = pd.to_numeric(x["close"], errors="coerce")
    x = x[close_num.notna() & (close_num > 0)].copy()
    x["close"] = pd.to_numeric(x["close"], errors="coerce")
    x["current_price"] = x["close"]

    price_col = first_existing_col(df, PRICE_CANDIDATES)

    logger.info(
        "[RANKING SUMMARY RUNNER] price normalized price_cols=%s rows=%s->%s close_nonnull=%s close_gt0=%s",
        [c for c in PRICE_CANDIDATES if c in df.columns],
        before,
        len(x),
        int(pd.to_numeric(df[price_col], errors="coerce").notna().sum()) if price_col else 0,
        int((pd.to_numeric(df[price_col], errors="coerce") > 0).sum()) if price_col else 0,
    )

    if x.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # OHLC
    # --------------------------------------------------------
    for c in ("open", "high", "low"):
        if c not in x.columns:
            x[c] = x["close"]
        else:
            x[c] = pd.to_numeric(x[c], errors="coerce").fillna(x["close"])

    # --------------------------------------------------------
    # volume
    # --------------------------------------------------------
    x["volume"] = combine_numeric_columns(x, VOLUME_CANDIDATES).fillna(0.0)
    x["trading_volume"] = x["volume"]

    # --------------------------------------------------------
    # turnover / trading_value
    # --------------------------------------------------------
    turnover = combine_numeric_columns(x, TURNOVER_CANDIDATES)

    if turnover.notna().any():
        x["turnover"] = turnover.fillna(0.0)
    else:
        x["turnover"] = x["close"] * x["volume"]

    x["trading_value"] = x["turnover"]

    # --------------------------------------------------------
    # rank
    # --------------------------------------------------------
    rank = combine_numeric_columns(x, RANK_CANDIDATES)

    if rank.notna().any():
        x["rank"] = rank
        x["rank_position"] = rank
        x["best_rank_position"] = rank
    else:
        x["rank"] = pd.NA
        x["rank_position"] = pd.NA
        x["best_rank_position"] = pd.NA

    # --------------------------------------------------------
    # ranking_type
    # --------------------------------------------------------
    x["ranking_type"] = combine_text_columns(
        x,
        TYPE_CANDIDATES,
        default="ranking",
    )

    x["rank_type"] = x["ranking_type"]
    x["category"] = x["ranking_type"]

    # --------------------------------------------------------
    # change percentage
    # --------------------------------------------------------
    x["change_percentage"] = combine_numeric_columns(x, CHANGE_CANDIDATES)

    # --------------------------------------------------------
    # market / market_type
    # --------------------------------------------------------
    if "market" not in x.columns:
        x["market"] = ""

    if "market_type" not in x.columns:
        x["market_type"] = ""

    # --------------------------------------------------------
    # optional numeric
    # --------------------------------------------------------
    for c in OPTIONAL_NUMERIC_COLS:
        if c in x.columns:
            try:
                x[c] = pd.to_numeric(x[c], errors="coerce")
            except Exception:
                pass

    # --------------------------------------------------------
    # duplicate guard
    # --------------------------------------------------------
    before = len(x)

    x = x.sort_values(
        ["symbol", "datetime"],
        ascending=[True, True],
        kind="mergesort",
    )

    x = x.drop_duplicates(
        subset=["symbol", "datetime", "ranking_type"],
        keep="last",
    )

    logger.info(
        "[RANKING SUMMARY RUNNER] duplicate drop rows=%s->%s",
        before,
        len(x),
    )

    log_df_profile("normalized", x)

    return x.reset_index(drop=True)


def filter_trade_date_if_possible(
    df: pd.DataFrame,
    *,
    trade_date: dt.date,
) -> pd.DataFrame:
    """
    当日DBに前日データ等が混ざる場合だけ当日データに寄せる。
    ただし当日抽出で空になる場合は、元データを返して全落ちを防ぐ。
    """
    if df is None or df.empty or "datetime" not in df.columns:
        return pd.DataFrame() if df is None else df

    try:
        x = df.copy()
        dates = pd.to_datetime(x["datetime"], errors="coerce").dt.date

        same_day = x[dates == trade_date].copy()

        if not same_day.empty:
            logger.info(
                "[RANKING SUMMARY RUNNER] trade_date filter applied date=%s rows=%s->%s",
                trade_date,
                len(x),
                len(same_day),
            )
            return same_day.reset_index(drop=True)

        logger.warning(
            "[RANKING SUMMARY RUNNER] trade_date filter skipped because same_day empty date=%s rows=%s dt_min=%s dt_max=%s",
            trade_date,
            len(x),
            x["datetime"].min(),
            x["datetime"].max(),
        )
        return x.reset_index(drop=True)

    except Exception:
        logger.exception("[RANKING SUMMARY RUNNER] trade_date filter failed")
        return df


def filter_lookback(
    df: pd.DataFrame,
    *,
    lookback_minutes: int,
) -> pd.DataFrame:
    """
    latest datetime から lookback_minutes 分だけ残す。
    ただし全落ちする場合は元データを返す。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    if "datetime" not in x.columns:
        return x

    latest_dt = pd.to_datetime(x["datetime"], errors="coerce").max()

    if pd.isna(latest_dt):
        return x

    try:
        lookback = int(lookback_minutes)
    except Exception:
        lookback = 240

    if lookback <= 0:
        return x

    start_dt = latest_dt - pd.Timedelta(minutes=lookback)
    y = x[pd.to_datetime(x["datetime"], errors="coerce") >= start_dt].copy()

    if y.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] lookback filter skipped because empty lookback=%s latest=%s start=%s rows=%s",
            lookback,
            latest_dt,
            start_dt,
            len(x),
        )
        return x.reset_index(drop=True)

    logger.info(
        "[RANKING SUMMARY RUNNER] lookback filter applied lookback=%s rows=%s->%s latest=%s start=%s",
        lookback,
        len(x),
        len(y),
        latest_dt,
        start_dt,
    )

    return y.reset_index(drop=True)


def filter_symbols(
    df: pd.DataFrame,
    symbols: Optional[Iterable[str]],
    *,
    fallback_if_empty: bool = True,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    symbol_list = normalize_symbols(symbols)

    if not symbol_list:
        return df.reset_index(drop=True)

    x = df.copy()
    x["symbol"] = x["symbol"].astype(str).str.strip()

    y = x[x["symbol"].isin(symbol_list)].copy()

    if y.empty and fallback_if_empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] symbol filter would empty dataframe -> fallback to unfiltered symbols_requested=%s rows=%s available_symbols=%s",
            len(symbol_list),
            len(x),
            x["symbol"].nunique(),
        )
        return x.reset_index(drop=True)

    logger.info(
        "[RANKING SUMMARY RUNNER] symbol filter applied rows=%s->%s symbols_requested=%s symbols_after=%s",
        len(x),
        len(y),
        len(symbol_list),
        y["symbol"].nunique() if not y.empty else 0,
    )

    return y.reset_index(drop=True)