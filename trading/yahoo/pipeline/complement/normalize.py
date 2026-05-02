# ============================================================
# File   : trading/yahoo/pipeline/complement/normalize.py
# Version: PRODUCTION-STABLE-REV4.1-YAHOO-COMPLEMENT-NORMALIZE
# ------------------------------------------------------------
# 【概要】
#   Yahoo 1分足DataFrameの正規化
#
# 【主な機能】
#   - DataFrame guard
#   - duplicate column除去
#   - symbol正規化
#   - datetime正規化
#   - OHLCV alias吸収
#   - symbolname補完
#   - symbol+datetime 重複除去
#
# 【入力想定】
#   - symbol / code / ticker / 銘柄コード
#   - datetime / time / Datetime / timestamp / 日時 / 日付
#   - open / Open / 始値
#   - high / High / 高値
#   - low / Low / 安値
#   - close / Close / price / 現在値 / 終値
#   - volume / Volume / 出来高
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from .constants import OHLCV_ALIAS_MAP, NUMERIC_COLUMNS

logger = logging.getLogger(__name__)


try:
    from global_state import global_data
except Exception:  # pragma: no cover
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None


# ============================================================
# basic guards
# ============================================================

def safe_df(df: Any) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()

        if isinstance(df, pd.DataFrame):
            out = df.copy()
        else:
            out = pd.DataFrame(df)

        if out.empty:
            return pd.DataFrame()

        try:
            out = out.loc[:, ~out.columns.duplicated()]
        except Exception:
            pass

        return out

    except Exception:
        logger.exception("[YAHOO NORMALIZE] dataframe guard failed")
        return pd.DataFrame()


def normalize_symbol_value(v: Any) -> str:
    try:
        if pd.isna(v):
            return ""
        s = str(v).strip()
        s = s.replace(".T", "")

        if s.endswith(".0"):
            s2 = s[:-2]
            if s2.isdigit():
                return s2

        return s
    except Exception:
        return ""


def normalize_datetime_df(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = safe_df(df)
        if out.empty:
            return out

        if "datetime" not in out.columns:
            logger.warning("[YAHOO NORMALIZE] datetime column missing cols=%s", list(out.columns))
            return pd.DataFrame()

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"])

        if out.empty:
            return pd.DataFrame()

        try:
            if getattr(out["datetime"].dt, "tz", None) is not None:
                try:
                    out["datetime"] = out["datetime"].dt.tz_convert(None)
                except Exception:
                    out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        out["datetime"] = out["datetime"].dt.floor("min")

        sort_cols = []
        if "symbol" in out.columns:
            sort_cols.append("symbol")
        sort_cols.append("datetime")

        out = out.sort_values(sort_cols, kind="stable").reset_index(drop=True)
        return out

    except Exception:
        logger.exception("[YAHOO NORMALIZE] datetime normalize failed")
        return pd.DataFrame()


def numeric_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if df is None or df.empty:
            return pd.Series(dtype="float64")
        if col not in df.columns:
            return pd.Series(default, index=df.index, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce")
    except Exception:
        logger.debug("[YAHOO NORMALIZE] numeric conversion failed col=%s", col, exc_info=True)
        return pd.Series(default, index=df.index, dtype="float64")


def coalesce_series(primary: pd.Series, secondary: pd.Series) -> pd.Series:
    try:
        return primary.where(primary.notna(), secondary)
    except Exception:
        try:
            return primary.combine_first(secondary)
        except Exception:
            return primary


# ============================================================
# symbolname
# ============================================================

def symbol_name_map() -> dict[str, str]:
    try:
        mp = getattr(global_data, "symbol_name_map", {}) if global_data is not None else {}
        if isinstance(mp, dict):
            return {str(k).strip(): str(v).strip() for k, v in mp.items()}
    except Exception:
        pass
    return {}


def backfill_symbolname(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        if "symbol" not in out.columns:
            return out

        if "symbolname" not in out.columns:
            out["symbolname"] = ""

        out["symbol"] = out["symbol"].map(normalize_symbol_value)
        out["symbolname"] = out["symbolname"].fillna("").astype(str).str.strip()

        bad = out["symbolname"].isin(["", "0", "0.0", "nan", "None", "<NA>"])

        mp = symbol_name_map()
        if mp:
            mapped = out["symbol"].map(lambda x: mp.get(str(x).strip(), ""))
            out["symbolname"] = out["symbolname"].mask(bad, mapped)
            out["symbolname"] = out["symbolname"].fillna("").astype(str).str.strip()

        bad = out["symbolname"].isin(["", "0", "0.0", "nan", "None", "<NA>"])
        out.loc[bad, "symbolname"] = out.loc[bad, "symbol"]

    except Exception:
        logger.debug("[YAHOO NORMALIZE] symbolname backfill failed", exc_info=True)

    return out


# ============================================================
# aliases
# ============================================================

def apply_ohlcv_aliases(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        # exact alias
        for src, dst in OHLCV_ALIAS_MAP.items():
            if src in out.columns and dst not in out.columns:
                out[dst] = out[src]

        # lowercase alias
        lower_map = {str(c).lower(): c for c in out.columns}
        extra = {
            "datetime": ["datetime", "date_time", "timestamp", "time"],
            "symbol": ["symbol", "code", "ticker"],
            "open": ["open", "open_price"],
            "high": ["high", "high_price"],
            "low": ["low", "low_price"],
            "close": ["close", "close_price", "price", "current_price", "last_price"],
            "volume": ["volume", "trading_volume", "tradingvolume"],
        }

        for dst, names in extra.items():
            if dst in out.columns:
                continue
            for name in names:
                c = lower_map.get(name.lower())
                if c is not None:
                    out[dst] = out[c]
                    break

    except Exception:
        logger.exception("[YAHOO NORMALIZE] apply aliases failed")

    return out


def normalize_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    for c in NUMERIC_COLUMNS:
        if c in out.columns:
            try:
                out[c] = pd.to_numeric(out[c], errors="coerce")
            except Exception:
                pass

    return out


# ============================================================
# main normalize
# ============================================================

def normalize_yahoo_1min_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Yahoo 1分足DataFrameを標準OHLCVへ正規化する。
    """
    out = safe_df(df)
    if out.empty:
        return out

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if str(x) != ""]).strip()
                for col in out.columns
            ]
    except Exception:
        pass

    # indexがDatetimeIndexの場合
    if "datetime" not in out.columns:
        try:
            if isinstance(out.index, pd.DatetimeIndex):
                out = out.reset_index()
                if "index" in out.columns:
                    out = out.rename(columns={"index": "datetime"})
        except Exception:
            pass

    out = apply_ohlcv_aliases(out)

    required = {"symbol", "datetime", "close"}
    if not required.issubset(out.columns):
        logger.warning(
            "[YAHOO NORMALIZE] missing required cols=%s actual=%s",
            sorted(required - set(out.columns)),
            list(out.columns),
        )
        return pd.DataFrame()

    out["symbol"] = out["symbol"].map(normalize_symbol_value)
    out = out[out["symbol"] != ""].copy()

    if out.empty:
        return pd.DataFrame()

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in out.columns:
            if col == "volume":
                out[col] = 0.0
            else:
                out[col] = pd.NA

    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    # open/high/low が欠ける場合は close で補完
    for col in ["open", "high", "low"]:
        out[col] = out[col].fillna(out["close"])

    out["volume"] = out["volume"].fillna(0.0)

    out = normalize_datetime_df(out)
    if out.empty:
        return out

    out = out.dropna(subset=["symbol", "close"])
    if out.empty:
        return pd.DataFrame()

    out = backfill_symbolname(out)

    out = (
        out.sort_values(["symbol", "datetime"], kind="stable")
           .drop_duplicates(subset=["symbol", "datetime"], keep="last")
           .reset_index(drop=True)
    )

    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["close_price"] = out["close"]

    out["price"] = out["close"]
    out["current_price"] = out["close"]
    out["trading_volume"] = out["volume"]

    logger.info(
        "[YAHOO NORMALIZE] normalized rows=%s symbols=%s dt_min=%s dt_max=%s",
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
        out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
    )

    return out


def drop_recent_rows(
    df: pd.DataFrame,
    touch_recent_minutes: int,
    *,
    now: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    try:
        out = safe_df(df)
        if out.empty:
            return out

        if not touch_recent_minutes or touch_recent_minutes <= 0:
            return out

        now_ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now()
        now_ts = now_ts.floor("min")
        cutoff = now_ts - pd.Timedelta(minutes=int(touch_recent_minutes))

        before = len(out)
        out = out[out["datetime"] <= cutoff].copy()

        logger.info(
            "[YAHOO NORMALIZE] recent filter rows=%s -> %s cutoff=%s touch_recent_minutes=%s",
            before,
            len(out),
            cutoff,
            touch_recent_minutes,
        )

        return out

    except Exception:
        logger.exception("[YAHOO NORMALIZE] recent-row filter failed")
        return pd.DataFrame()


__all__ = [
    "safe_df",
    "normalize_symbol_value",
    "normalize_datetime_df",
    "numeric_series",
    "coalesce_series",
    "symbol_name_map",
    "backfill_symbolname",
    "apply_ohlcv_aliases",
    "normalize_numeric_columns",
    "normalize_yahoo_1min_df",
    "drop_recent_rows",
]