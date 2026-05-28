# ============================================================
# File   : trading/ranking/summary/bootstrap_ohlcv.py
# Version: Ver1.1-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-OHLCV-DATETIME-FORMAT
# ------------------------------------------------------------
# 【概要】
#   ranking snapshot から 1min 擬似OHLCVを作成
#
# 【重要方針】
#   ranking snapshot は約定足ではないため、
#   open = high = low = close = snapshot price
#
# Ver1.1:
#   - pd.to_datetime の自動format推定Warningを抑制
#   - よく使う日時形式を明示的に順番parse
# ============================================================

from __future__ import annotations

import logging
import warnings
from typing import Iterable

import numpy as np
import pandas as pd

from trading.ranking.summary.bootstrap_config import (
    PRICE_CANDIDATES,
    RANK_POSITION_CANDIDATES,
    RANK_TYPE_CANDIDATES,
    SYMBOLNAME_CANDIDATES,
    TICK_COUNT_CANDIDATES,
    TRADING_VALUE_CANDIDATES,
    VOLUME_CANDIDATES,
)

logger = logging.getLogger(__name__)

_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
    "%H:%M:%S",
    "%H:%M",
)


def safe_numeric_series(
    df: pd.DataFrame,
    col: str,
    *,
    default: float | None = np.nan,
    fill: bool = False,
) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="float64")

    if col not in df.columns:
        if default is None:
            return pd.Series(np.nan, index=df.index, dtype="float64")
        return pd.Series(float(default), index=df.index, dtype="float64")

    try:
        s = df[col]

        if isinstance(s, pd.DataFrame):
            if s.shape[1] == 0:
                if default is None:
                    return pd.Series(np.nan, index=df.index, dtype="float64")
                return pd.Series(float(default), index=df.index, dtype="float64")
            s = s.iloc[:, 0]

        if getattr(s, "dtype", None) == object:
            s = (
                s.astype(str)
                .str.replace(",", "", regex=False)
                .str.replace("円", "", regex=False)
                .str.strip()
                .replace(
                    {
                        "": np.nan,
                        "None": np.nan,
                        "none": np.nan,
                        "NULL": np.nan,
                        "null": np.nan,
                        "nan": np.nan,
                        "NaN": np.nan,
                        "<NA>": np.nan,
                        "pd.NA": np.nan,
                    }
                )
            )

        out = pd.to_numeric(s, errors="coerce")

        if not isinstance(out, pd.Series):
            out = pd.Series(out, index=df.index, dtype="float64")

        out = out.astype("float64")

        if fill:
            if default is None:
                out = out.fillna(np.nan)
            else:
                out = out.fillna(float(default))

        return out

    except Exception:
        logger.exception("[RANKING SUMMARY BOOTSTRAP OHLCV] numeric conversion failed col=%s", col)
        if default is None:
            return pd.Series(np.nan, index=df.index, dtype="float64")
        return pd.Series(float(default), index=df.index, dtype="float64")


def _normalize_datetime_text(s: pd.Series) -> pd.Series:
    try:
        return (
            s.astype(str)
            .str.strip()
            .str.replace("/", "-", regex=False)
            .str.replace("T", " ", regex=False)
            .str.replace("Z", "", regex=False)
            .replace(
                {
                    "": np.nan,
                    "None": np.nan,
                    "none": np.nan,
                    "NULL": np.nan,
                    "null": np.nan,
                    "nan": np.nan,
                    "NaN": np.nan,
                    "<NA>": np.nan,
                    "pd.NA": np.nan,
                    "NaT": np.nan,
                }
            )
        )
    except Exception:
        return s


def _parse_datetime_series(s: pd.Series, *, index: pd.Index) -> pd.Series:
    """Warningを出さずに日時Seriesをparseする。

    pandasの自動推定Warningを避けるため、既知formatを順番に試す。
    残りだけ最後に warnings 抑制付きでfallback parseする。
    """
    if s is None:
        return pd.Series(pd.NaT, index=index)

    try:
        if pd.api.types.is_datetime64_any_dtype(s):
            return pd.to_datetime(s, errors="coerce")
    except Exception:
        pass

    raw = _normalize_datetime_text(pd.Series(s, index=index))
    out = pd.Series(pd.NaT, index=index, dtype="datetime64[ns]")

    # Excel/Unix serialのような数値は通常入らない想定だが、念のため秒/ミリ秒epochだけ拾う。
    try:
        numeric = pd.to_numeric(raw, errors="coerce")
        num_mask = numeric.notna() & raw.astype(str).str.match(r"^\d+(\.0+)?$", na=False)
        if bool(num_mask.any()):
            # 13桁以上はms、それ以外は秒扱い。ただしYYYYMMDDHHMM系はformat側に任せる。
            lens = raw.astype(str).str.replace(r"\.0+$", "", regex=True).str.len()
            epoch_mask = num_mask & lens.isin([10, 13])
            if bool(epoch_mask.any()):
                unit = "ms" if int(lens[epoch_mask].max()) >= 13 else "s"
                out.loc[epoch_mask] = pd.to_datetime(numeric.loc[epoch_mask], unit=unit, errors="coerce")
    except Exception:
        pass

    remaining = out.isna() & raw.notna()
    for fmt in _DATETIME_FORMATS:
        if not bool(remaining.any()):
            break
        try:
            parsed = pd.to_datetime(raw.loc[remaining], format=fmt, errors="coerce")
            hit = parsed.notna()
            if bool(hit.any()):
                out.loc[parsed.index[hit]] = parsed.loc[hit]
                remaining = out.isna() & raw.notna()
        except Exception:
            continue

    if bool(remaining.any()):
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Could not infer format.*", category=UserWarning)
                parsed = pd.to_datetime(raw.loc[remaining], errors="coerce")
            out.loc[parsed.index] = parsed
        except Exception:
            pass

    return out


def safe_datetime_series(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="datetime64[ns]")

    if col not in df.columns:
        return pd.Series(pd.NaT, index=df.index)

    try:
        s = df[col]
        if isinstance(s, pd.DataFrame):
            if s.shape[1] == 0:
                return pd.Series(pd.NaT, index=df.index)
            s = s.iloc[:, 0]
        return _parse_datetime_series(pd.Series(s, index=df.index), index=df.index)
    except Exception:
        logger.exception("[RANKING SUMMARY BOOTSTRAP OHLCV] datetime conversion failed col=%s", col)
        return pd.Series(pd.NaT, index=df.index)


def combine_numeric_columns(
    df: pd.DataFrame,
    candidates: Iterable[str],
    *,
    default: float | None = np.nan,
    fill: bool = False,
) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="float64")

    out = pd.Series(np.nan, index=df.index, dtype="float64")

    for col in candidates:
        if col not in df.columns:
            continue
        s = safe_numeric_series(df, col, default=np.nan, fill=False)
        out = out.where(out.notna(), s)

    if fill:
        if default is None:
            out = out.fillna(np.nan)
        else:
            out = out.fillna(float(default))

    return out.astype("float64")


def combine_text_columns(
    df: pd.DataFrame,
    candidates: Iterable[str],
    *,
    default: str = "",
) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="object")

    out = pd.Series(default, index=df.index, dtype="object")

    for col in candidates:
        if col not in df.columns:
            continue

        try:
            s = df[col]
            if isinstance(s, pd.DataFrame):
                if s.shape[1] == 0:
                    continue
                s = s.iloc[:, 0]

            s = s.astype(str).replace(
                {
                    "nan": "",
                    "NaN": "",
                    "None": "",
                    "<NA>": "",
                    "pd.NA": "",
                }
            )

            mask = out.astype(str).str.len().eq(0) & s.astype(str).str.len().gt(0)
            out.loc[mask] = s.loc[mask]
        except Exception:
            continue

    return out


def normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    x = df.copy()

    if "symbol" not in x.columns:
        for c in ["Symbol", "銘柄コード", "code", "Code"]:
            if c in x.columns:
                x["symbol"] = x[c]
                break

    if "symbol" not in x.columns:
        x["symbol"] = ""

    x["symbol"] = x["symbol"].astype(str).str.strip()
    x = x[x["symbol"].str.len() > 0].copy()

    return x


def normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    x = df.copy()

    if "datetime" not in x.columns:
        for c in ["inserted_at", "time", "timestamp", "created_at", "end_time"]:
            if c in x.columns:
                x["datetime"] = x[c]
                break

    x["datetime"] = safe_datetime_series(x, "datetime")

    if "end_time" in x.columns:
        end_time = safe_datetime_series(x, "end_time")
        x["datetime"] = x["datetime"].fillna(end_time)

    x = x.dropna(subset=["datetime"]).copy()
    x["datetime"] = safe_datetime_series(x, "datetime").dt.floor("min")
    x = x.dropna(subset=["datetime"]).copy()

    return x


def _join_rank_types(s: pd.Series) -> str:
    vals: list[str] = []

    for v in s.astype(str).tolist():
        for part in str(v).split(","):
            part = part.strip()
            if part and part not in vals and part not in ("nan", "None", "<NA>"):
                vals.append(part)

    return ",".join(vals)


def build_pseudo_ohlcv_1min_from_snapshot(snapshot_df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking_snapshot から 1min ranking summary base を作る。

    OHLC 方針:
      open = high = low = close = snapshot price
    """
    if snapshot_df is None or snapshot_df.empty:
        return pd.DataFrame()

    x = snapshot_df.copy()
    x = normalize_symbol(x)
    x = normalize_datetime(x)

    if x.empty:
        return pd.DataFrame()

    x["price"] = combine_numeric_columns(x, PRICE_CANDIDATES, default=np.nan)
    x["volume_raw"] = combine_numeric_columns(x, VOLUME_CANDIDATES, default=0.0, fill=True)
    x["trading_value_raw"] = combine_numeric_columns(x, TRADING_VALUE_CANDIDATES, default=0.0, fill=True)
    x["tick_count_raw"] = combine_numeric_columns(x, TICK_COUNT_CANDIDATES, default=0.0, fill=True)
    x["rank_position_raw"] = combine_numeric_columns(x, RANK_POSITION_CANDIDATES, default=np.nan)
    x["rank_type_raw"] = combine_text_columns(x, RANK_TYPE_CANDIDATES, default="")
    x["symbolname_raw"] = combine_text_columns(x, SYMBOLNAME_CANDIDATES, default="")

    x = x.sort_values(["symbol", "datetime"]).copy()

    grouped = x.groupby(["symbol", "datetime"], sort=False)

    out = grouped.agg(
        symbolname=("symbolname_raw", "last"),
        close=("price", "last"),
        volume=("volume_raw", "max"),
        trading_value=("trading_value_raw", "max"),
        tick_count=("tick_count_raw", "max"),
        best_rank_position=("rank_position_raw", "min"),
        last_rank_position=("rank_position_raw", "last"),
        avg_rank_position=("rank_position_raw", "mean"),
        rank_count=("rank_position_raw", "count"),
        rank_types=("rank_type_raw", _join_rank_types),
    ).reset_index()

    out["open"] = out["close"]
    out["high"] = out["close"]
    out["low"] = out["close"]

    try:
        denom = pd.to_numeric(out["volume"], errors="coerce").replace(0, np.nan)
        out["turnover"] = pd.to_numeric(out["trading_value"], errors="coerce") / denom
    except Exception:
        out["turnover"] = pd.Series(np.nan, index=out.index, dtype="float64")

    out["interval"] = 1
    out["source"] = "ranking_snapshot"
    out["updated_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info(
        "[RANKING SUMMARY BOOTSTRAP OHLCV] 1min built rows=%d symbols=%d dt_min=%s dt_max=%s close_nonnull=%d",
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["datetime"].min() if not out.empty else None,
        out["datetime"].max() if not out.empty else None,
        int(pd.to_numeric(out["close"], errors="coerce").notna().sum()) if "close" in out.columns else 0,
    )

    return out


__all__ = [
    "safe_numeric_series",
    "safe_datetime_series",
    "combine_numeric_columns",
    "combine_text_columns",
    "normalize_symbol",
    "normalize_datetime",
    "build_pseudo_ohlcv_1min_from_snapshot",
]
