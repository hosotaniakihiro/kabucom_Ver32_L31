# ============================================================
# confirmed_bar_builder.py
# Ver26.9-PRODUCTION-HARDENED
# ------------------------------------------------------------
# ✔ PUSH → 確定 1分足（OHLCV 正本）
# ✔ RANKING スナップショット由来 疑似1m 補完
# ✔ PUSH 優先マージ（疑似は補助のみ）
# ✔ price alias repair（ranking完全互換）
# ✔ end_time / datetime 正本厳守
# ✔ summary / indicator / ATR / EMA 完全互換
# ✔ DB保存可能な DataFrame 形式
# ✔ symbolname 補完
# ✔ pandas 安定ソート
# ✔ ranking snapshot 多形式対応
# ✔ NaN / dtype sanitize
# ✔ production hardened
# ============================================================

import pandas as pd
import numpy as np
import logging
from typing import Optional

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_df(df):

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    return df.copy()


def _sanitize_numeric(df):

    if df.empty:
        return df

    num_cols = df.select_dtypes(include=np.number).columns

    df[num_cols] = (
        df[num_cols]
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )

    return df


# ============================================================
# Public API
# ============================================================

def build_confirmed_1min_from_push(
    *args,
    df_push: Optional[pd.DataFrame] = None,
    cutoff_time: Optional[pd.Timestamp] = None,
    **kwargs,
) -> pd.DataFrame:

    # --------------------------------------------------------
    # 引数互換
    # --------------------------------------------------------

    if args:

        if df_push is None and len(args) >= 1:
            df_push = args[0]

        if cutoff_time is None and len(args) >= 2:
            cutoff_time = args[1]

    if df_push is None:
        df_push = getattr(global_data, "push_df", None)

    df_push = _safe_df(df_push)

    # --------------------------------------------------------
    # PUSH 1min
    # --------------------------------------------------------

    df_push_1m = _build_1m_from_tick_df(
        df_tick=df_push,
        cutoff_time=cutoff_time,
        source="PUSH",
    )

    # --------------------------------------------------------
    # ranking pseudo
    # --------------------------------------------------------

    ranking_df = getattr(global_data, "latest_ranking_snapshot", None)

    df_rank_1m = _build_1m_from_ranking_snapshot(
        df_ranking=ranking_df,
        cutoff_time=cutoff_time,
    )

    # --------------------------------------------------------
    # merge
    # --------------------------------------------------------

    if df_push_1m.empty:

        merged = df_rank_1m

    elif df_rank_1m.empty:

        merged = df_push_1m

    else:

        merged = pd.concat(
            [df_push_1m, df_rank_1m],
            ignore_index=True
        )

        merged["__source_pri"] = (
            merged["source"]
            .map({"PUSH": 0, "RANKING_PSEUDO": 1})
            .fillna(9)
        )

        merged = (
            merged
            .sort_values(["symbol", "end_time", "__source_pri"])
            .drop_duplicates(
                subset=["symbol", "end_time"],
                keep="first"
            )
            .drop(columns="__source_pri")
        )

    if merged.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # symbolname
    # --------------------------------------------------------

    symbol_map = getattr(global_data, "symbol_name_map", {}) or {}

    merged["symbolname"] = merged["symbol"].map(
        lambda s: symbol_map.get(str(s), "")
    )

    # --------------------------------------------------------
    # meta columns
    # --------------------------------------------------------

    merged["date"] = merged["end_time"].dt.normalize().dt.date
    merged["time"] = merged["end_time"].dt.time

    merged["time_range"] = (
        merged["start_time"].dt.strftime("%H:%M")
        + " - "
        + merged["end_time"].dt.strftime("%H:%M")
    )

    merged["interval"] = 1
    merged["interval_name"] = "1min"

    # --------------------------------------------------------
    # sanitize
    # --------------------------------------------------------

    merged = _sanitize_numeric(merged)

    # --------------------------------------------------------
    # final sort
    # --------------------------------------------------------

    merged = (
        merged
        .sort_values(["symbol", "end_time"])
        .reset_index(drop=True)
    )

    logger.info(
        "[CONFIRM1M] rows=%d symbols=%d push=%d ranking=%d",
        len(merged),
        merged["symbol"].nunique(),
        int((merged["source"] == "PUSH").sum()),
        int((merged["source"] == "RANKING_PSEUDO").sum()),
    )

    return merged


# ============================================================
# PUSH → 1min
# ============================================================

def _build_1m_from_tick_df(
    df_tick: pd.DataFrame,
    cutoff_time: Optional[pd.Timestamp],
    source: str,
) -> pd.DataFrame:

    df_tick = _safe_df(df_tick)

    if df_tick.empty:
        return pd.DataFrame()

    required = {"symbol", "price", "volume"}

    if not required.issubset(df_tick.columns):
        return pd.DataFrame()

    df = df_tick.copy()

    if "time" in df.columns:
        df["datetime"] = pd.to_datetime(df["time"], errors="coerce")
    else:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    df = df.dropna(subset=["datetime", "price"])

    if df.empty:
        return pd.DataFrame()

    if cutoff_time is not None:
        df = df[df["datetime"] <= cutoff_time]
    else:
        df = df[df["datetime"] < pd.Timestamp.now().floor("1min")]

    if df.empty:
        return pd.DataFrame()

    df["symbol"] = df["symbol"].astype(str)
    df["t_floor"] = df["datetime"].dt.floor("1min")

    out = (
        df.groupby(["symbol", "t_floor"], as_index=False)
        .agg(
            open_price=("price", "first"),
            high_price=("price", "max"),
            low_price=("price", "min"),
            close_price=("price", "last"),
            volume=("volume", "sum"),
        )
    )

    out["start_time"] = out["t_floor"]
    out["end_time"] = out["t_floor"] + pd.Timedelta(minutes=1)
    out["datetime"] = out["end_time"]
    out["interval"] = 1
    out["interval_name"] = "1min"
    out["source"] = source

    return out


# ============================================================
# ranking snapshot → pseudo 1min
# ============================================================

def _build_1m_from_ranking_snapshot(
    df_ranking: Optional[pd.DataFrame],
    cutoff_time: Optional[pd.Timestamp],
) -> pd.DataFrame:

    df_ranking = _safe_df(df_ranking)

    if df_ranking.empty:
        return pd.DataFrame()

    df = df_ranking.copy()

    # --------------------------------------------------------
    # price alias repair
    # --------------------------------------------------------

    if "price" not in df.columns:

        for c in (
            "current_price",
            "close_price",
            "last_price",
            "price_now"
        ):
            if c in df.columns:
                df["price"] = df[c]
                break

    required = {"symbol", "price", "datetime"}

    if not required.issubset(df.columns):
        return pd.DataFrame()

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")

    df = df.dropna(subset=["datetime", "price"])

    if df.empty:
        return pd.DataFrame()

    if cutoff_time is not None:
        df = df[df["datetime"] <= cutoff_time]
    else:
        df = df[df["datetime"] < pd.Timestamp.now().floor("1min")]

    if df.empty:
        return pd.DataFrame()

    df["symbol"] = df["symbol"].astype(str)
    df["t_floor"] = df["datetime"].dt.floor("1min")

    out = (
        df.groupby(["symbol", "t_floor"], as_index=False)
        .agg(
            open_price=("price", "first"),
            high_price=("price", "max"),
            low_price=("price", "min"),
            close_price=("price", "last"),
        )
    )

    # pseudo volume
    out["volume"] = 1

    out["start_time"] = out["t_floor"]
    out["end_time"] = out["t_floor"] + pd.Timedelta(minutes=1)
    out["datetime"] = out["end_time"]
    out["interval"] = 1
    out["interval_name"] = "1min"
    out["source"] = "RANKING_PSEUDO"

    return out