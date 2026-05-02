# ============================================================
# File   : trading/ranking/summary/builder.py
# Version: PRODUCTION-STABLE-REV2.0-RANKING-SUMMARY-BUILDER
# Purpose:
#   ranking_snapshot_1min から ranking_summary_1min/3min/5min 用
#   DataFrame を生成する
#
# Important:
#   - ランキング由来は擬似OHLC
#   - open = high = low = close
#   - 本物ATRは作らない
#   - ranking専用特徴量は features.py で付与する
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


try:
    from trading.ranking.summary.features import add_ranking_only_features
except Exception:  # pragma: no cover
    add_ranking_only_features = None


# ============================================================
# basic helpers
# ============================================================

def _to_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _first_existing_col(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _ensure_col(df: pd.DataFrame, col: str, default: Any = np.nan) -> None:
    if col not in df.columns:
        df[col] = default


def _normalize_symbol(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "time" in out.columns and "date" in out.columns:
        out["datetime"] = pd.to_datetime(
            out["date"].astype(str) + " " + out["time"].astype(str),
            errors="coerce",
        )
    elif "timestamp" in out.columns:
        out["datetime"] = pd.to_datetime(out["timestamp"], errors="coerce")
    else:
        out["datetime"] = pd.NaT

    out = out.dropna(subset=["datetime"])
    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

    return out


# ============================================================
# snapshot normalize
# ============================================================

def normalize_ranking_snapshot_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking_snapshot_1min / ranking_raw 系の列名差を吸収する。
    """

    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = _normalize_datetime(out)

    symbol_col = _first_existing_col(
        out,
        [
            "symbol",
            "Symbol",
            "code",
            "Code",
            "銘柄コード",
        ],
    )
    name_col = _first_existing_col(
        out,
        [
            "symbolname",
            "symbol_name",
            "name",
            "Name",
            "銘柄名",
        ],
    )
    price_col = _first_existing_col(
        out,
        [
            "current_price",
            "price",
            "close",
            "close_price",
            "CurrentPrice",
            "現在値",
        ],
    )
    rank_col = _first_existing_col(
        out,
        [
            "rank",
            "ranking",
            "Rank",
            "順位",
        ],
    )
    volume_col = _first_existing_col(
        out,
        [
            "volume",
            "Volume",
            "出来高",
            "trading_volume",
        ],
    )
    ranking_type_col = _first_existing_col(
        out,
        [
            "ranking_type",
            "type",
            "ranking_name",
            "category",
            "ランキング種別",
        ],
    )
    market_col = _first_existing_col(
        out,
        [
            "market",
            "exchange",
            "division",
            "市場",
        ],
    )

    if symbol_col is None:
        logger.warning("[RANKING SUMMARY BUILDER] symbol column not found")
        return pd.DataFrame()

    if price_col is None:
        logger.warning("[RANKING SUMMARY BUILDER] price column not found")
        return pd.DataFrame()

    out["symbol"] = out[symbol_col].map(_normalize_symbol)
    out["symbolname"] = out[name_col].astype(str) if name_col else ""
    out["close"] = _to_numeric(out[price_col])

    if rank_col:
        out["rank"] = _to_numeric(out[rank_col])
    else:
        out["rank"] = np.nan

    if volume_col:
        out["volume"] = _to_numeric(out[volume_col]).fillna(0.0)
    else:
        out["volume"] = 0.0

    if ranking_type_col:
        out["ranking_type"] = out[ranking_type_col].astype(str)
    else:
        out["ranking_type"] = ""

    if market_col:
        out["market"] = out[market_col].astype(str)
    else:
        out["market"] = ""

    out = out.dropna(subset=["close"])
    out = out[out["symbol"].astype(str) != ""]
    out = out[out["close"] > 0]

    # 同一 symbol/datetime は最後を採用
    out = out.sort_values(["symbol", "datetime"])
    out = out.drop_duplicates(
        ["symbol", "datetime", "ranking_type", "market"],
        keep="last",
    )

    return out


# ============================================================
# pseudo OHLC
# ============================================================

def build_pseudo_ohlc_1min(snapshot_df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking_snapshot から 1分擬似OHLCを作る。

    ランキングは1点価格しかないため:
      open = high = low = close
    """

    base = normalize_ranking_snapshot_df(snapshot_df)

    if base.empty:
        return base

    out = base.copy()

    out["open"] = out["close"]
    out["high"] = out["close"]
    out["low"] = out["close"]

    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["close_price"] = out["close"]

    out["source"] = "ranking_snapshot"

    cols = [
        "symbol",
        "symbolname",
        "datetime",
        "date",
        "time",
        "ranking_type",
        "market",
        "rank",
        "open",
        "high",
        "low",
        "close",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "source",
    ]

    for c in cols:
        _ensure_col(out, c)

    out = out[cols].copy()
    out = out.sort_values(["symbol", "datetime"])

    logger.info(
        "[RANKING SUMMARY BUILDER] pseudo 1min built rows=%s symbols=%s",
        len(out),
        out["symbol"].nunique(),
    )

    return out


# ============================================================
# indicators
# ============================================================

def add_light_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    ランキング由来用の軽量MA/RSI/MACDを付与する。
    本物OHLCではないため参考値。
    """

    if df is None or df.empty:
        return df

    out = df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.sort_values(["symbol", "datetime"])

    out["close"] = _to_numeric(out["close"])

    g = out.groupby("symbol", group_keys=False)

    out["ma5"] = g["close"].transform(lambda s: s.rolling(5, min_periods=1).mean())
    out["ma25"] = g["close"].transform(lambda s: s.rolling(25, min_periods=1).mean())
    out["ma75"] = g["close"].transform(lambda s: s.rolling(75, min_periods=1).mean())

    delta = g["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.groupby(out["symbol"]).transform(
        lambda s: s.rolling(14, min_periods=3).mean()
    )
    avg_loss = loss.groupby(out["symbol"]).transform(
        lambda s: s.rolling(14, min_periods=3).mean()
    )

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi"] = 100 - (100 / (1 + rs))
    out["rsi"] = out["rsi"].replace([np.inf, -np.inf], np.nan).fillna(50.0)

    ema12 = g["close"].transform(lambda s: s.ewm(span=12, adjust=False).mean())
    ema26 = g["close"].transform(lambda s: s.ewm(span=26, adjust=False).mean())

    out["macd"] = ema12 - ema26
    out["signal"] = out.groupby("symbol")["macd"].transform(
        lambda s: s.ewm(span=9, adjust=False).mean()
    )
    out["hist"] = out["macd"] - out["signal"]

    # 本物ATRではないため明示的に0
    out["atr"] = 0.0
    out["slope_atr_scaled"] = 0.0

    return out


# ============================================================
# ranking score compatibility
# ============================================================

def add_compat_scores(df: pd.DataFrame) -> pd.DataFrame:
    """
    既存表示・AI候補処理が score 系カラムを期待する場合の互換カラム。
    """

    if df is None or df.empty:
        return df

    out = df.copy()

    if "ranking_score" not in out.columns:
        out["ranking_score"] = 0.0

    out["score_buy"] = out["ranking_score"].clip(lower=0)
    out["score_sell"] = (-out["ranking_score"]).clip(lower=0)
    out["score_total"] = out["score_buy"] - out["score_sell"]
    out["final_score"] = out["score_total"]
    out["display_score"] = out["final_score"]
    out["score"] = out["final_score"]

    # ranking専用では slope/mtf は本判定に使わない
    if "slope" not in out.columns:
        out["slope"] = 0.0
    if "mtf" not in out.columns:
        out["mtf"] = 0.0
    if "score_mtf" not in out.columns:
        out["score_mtf"] = 0.0

    return out


# ============================================================
# resample
# ============================================================

def resample_ranking_summary(
    df_1min: pd.DataFrame,
    *,
    interval: int,
) -> pd.DataFrame:
    """
    ranking_summary_1min から 3min/5min を作る。
    """

    if df_1min is None or df_1min.empty:
        return pd.DataFrame()

    if interval == 1:
        return df_1min.copy()

    if interval not in (3, 5):
        raise ValueError(f"unsupported interval: {interval}")

    df = df_1min.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    df = df.sort_values(["symbol", "datetime"])

    freq = f"{interval}min"
    rows: list[pd.DataFrame] = []

    group_cols = ["symbol"]
    if "ranking_type" in df.columns:
        group_cols.append("ranking_type")
    if "market" in df.columns:
        group_cols.append("market")

    for _, g in df.groupby(group_cols, dropna=False):
        g = g.set_index("datetime").sort_index()

        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "last",
            "rank": "last",
            "symbolname": "last",
            "source": "last",
        }

        for c in [
            "ranking_type",
            "market",
            "price_delta",
            "price_delta_pct",
            "ranking_atr_proxy",
            "ranking_momentum",
            "rank_improve",
            "volume_delta",
            "ranking_score",
            "ma5",
            "ma25",
            "ma75",
            "rsi",
            "macd",
            "signal",
            "hist",
            "score_buy",
            "score_sell",
            "score_total",
            "final_score",
            "display_score",
            "score",
        ]:
            if c in g.columns and c not in agg:
                agg[c] = "last"

        res = g.resample(freq, label="right", closed="right").agg(agg)
        res = res.dropna(subset=["close"])
        res["symbol"] = g["symbol"].iloc[0]

        rows.append(res.reset_index())

    if not rows:
        return pd.DataFrame()

    out = pd.concat(rows, ignore_index=True)

    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["close_price"] = out["close"]

    out["source"] = f"ranking_summary_resample_{interval}min"

    out = out.sort_values(["symbol", "datetime"])
    out = out.drop_duplicates(["symbol", "datetime"], keep="last")

    logger.info(
        "[RANKING SUMMARY BUILDER] resampled interval=%s rows=%s symbols=%s",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
    )

    return out


# ============================================================
# main build functions
# ============================================================

def build_ranking_summary_1min(
    snapshot_df: pd.DataFrame,
    *,
    add_indicators: bool = True,
    add_features: bool = True,
) -> pd.DataFrame:
    """
    ranking_snapshot_1min から ranking_summary_1min を作る。
    """

    df = build_pseudo_ohlc_1min(snapshot_df)

    if df.empty:
        return df

    if add_indicators:
        df = add_light_indicators(df)

    if add_features and add_ranking_only_features is not None:
        df = add_ranking_only_features(df)

    df = add_compat_scores(df)

    df["source"] = "ranking_summary_1min"

    logger.info(
        "[RANKING SUMMARY BUILDER] build 1min done rows=%s symbols=%s score_nonzero=%s",
        len(df),
        df["symbol"].nunique() if "symbol" in df.columns else 0,
        int((df.get("ranking_score", 0) != 0).sum()) if "ranking_score" in df.columns else 0,
    )

    return df


def build_ranking_summary(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
) -> pd.DataFrame:
    """
    interval=1/3/5 の ranking_summary を作る。
    """

    df1 = build_ranking_summary_1min(snapshot_df)

    if interval == 1:
        return df1

    return resample_ranking_summary(df1, interval=interval)


def build_all_ranking_summaries(
    snapshot_df: pd.DataFrame,
) -> dict[int, pd.DataFrame]:
    """
    1min / 3min / 5min をまとめて作る。
    """

    df1 = build_ranking_summary_1min(snapshot_df)
    df3 = resample_ranking_summary(df1, interval=3) if not df1.empty else pd.DataFrame()
    df5 = resample_ranking_summary(df1, interval=5) if not df1.empty else pd.DataFrame()

    return {
        1: df1,
        3: df3,
        5: df5,
    }


# ============================================================
# compatibility aliases
# ============================================================

def build_from_snapshot(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
) -> pd.DataFrame:
    return build_ranking_summary(snapshot_df, interval=interval)


def build_summary_from_snapshot(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
) -> pd.DataFrame:
    return build_ranking_summary(snapshot_df, interval=interval)


def build_ranking_summary_df(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
) -> pd.DataFrame:
    return build_ranking_summary(snapshot_df, interval=interval)


__all__ = [
    "normalize_ranking_snapshot_df",
    "build_pseudo_ohlc_1min",
    "add_light_indicators",
    "add_compat_scores",
    "resample_ranking_summary",
    "build_ranking_summary_1min",
    "build_ranking_summary",
    "build_all_ranking_summaries",
    "build_from_snapshot",
    "build_summary_from_snapshot",
    "build_ranking_summary_df",
]