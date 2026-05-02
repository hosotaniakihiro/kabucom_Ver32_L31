# ============================================================
# File   : trading/summary/summary_indicator.py
# Version: Ver3.0-PRODUCTION-FULL-COMPAT-BRIDGE
# ------------------------------------------------------------
# ✔ apply_summary_indicators 提供
# ✔ 旧summary indicator呼び出し互換維持
# ✔ OHLC列名ゆらぎ吸収
# ✔ symbol / datetime normalize
# ✔ close/open/high/low と *_price 両対応
# ✔ add_all_indicators へ安全ブリッジ
# ✔ DataFrame hardening
# ✔ duplicate column guard
# ✔ NaN / inf safety
# ✔ 本番用完全版
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

from trading.summary.indicators.indicator_calculator import add_all_indicators

logger = logging.getLogger(__name__)


# ============================================================
# dataframe helpers
# ============================================================

def _ensure_dataframe(df: Any) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        out = df.copy()
    elif isinstance(df, pd.Series):
        out = pd.DataFrame([df.to_dict()])
    elif isinstance(df, dict):
        out = pd.DataFrame([df])
    else:
        try:
            out = pd.DataFrame(df)
        except Exception:
            logger.exception("[summary_indicator] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if str(x) != ""])
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        pass

    try:
        if out.columns.duplicated().any():
            dup = out.columns[out.columns.duplicated()].tolist()
            logger.warning("[summary_indicator] duplicate columns removed: %s", dup)
            out = out.loc[:, ~out.columns.duplicated()]
    except Exception:
        pass

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass

    return out


def _sanitize_inf_nan(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    try:
        df.replace([np.inf, -np.inf], np.nan, inplace=True)
    except Exception:
        pass

    return df


# ============================================================
# column normalize
# ============================================================

def _normalize_price_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    alias_pairs = [
        ("open", "open_price"),
        ("high", "high_price"),
        ("low", "low_price"),
        ("close", "close_price"),
    ]

    for a, b in alias_pairs:
        if a in df.columns and b not in df.columns:
            df[b] = df[a]
        if b in df.columns and a not in df.columns:
            df[a] = df[b]

    return df


def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    try:
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        elif "date" in df.columns and "time" in df.columns:
            df["datetime"] = pd.to_datetime(
                df["date"].astype(str) + " " + df["time"].astype(str),
                errors="coerce",
            )

        elif "date" in df.columns:
            df["datetime"] = pd.to_datetime(df["date"], errors="coerce")
    except Exception:
        logger.exception("[summary_indicator] datetime normalize failed")

    return df


def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    try:
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str).str.strip()
    except Exception:
        logger.exception("[summary_indicator] symbol normalize failed")

    return df


def _sort_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    sort_cols = []
    if "symbol" in df.columns:
        sort_cols.append("symbol")
    if "datetime" in df.columns:
        sort_cols.append("datetime")

    if not sort_cols:
        return df

    try:
        return df.sort_values(sort_cols).reset_index(drop=True)
    except Exception:
        logger.exception("[summary_indicator] sort failed")
        return df


def _to_numeric_if_exists(df: pd.DataFrame, cols) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    for c in cols:
        if c in df.columns:
            try:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            except Exception:
                pass

    return df


# ============================================================
# interval resolver
# ============================================================

def _resolve_interval_label(
    df: pd.DataFrame,
    interval: Optional[int | str] = None
) -> str:
    try:
        if interval is not None:
            s = str(interval).strip().lower()
            mapping = {
                "1": "1min",
                "3": "3min",
                "5": "5min",
                "1m": "1min",
                "3m": "3min",
                "5m": "5min",
                "1min": "1min",
                "3min": "3min",
                "5min": "5min",
            }
            return mapping.get(s, "1min")

        if "time_range" in df.columns and not df["time_range"].dropna().empty:
            tr = str(df["time_range"].dropna().iloc[-1]).strip().lower()
            mapping = {
                "1": "1min",
                "3": "3min",
                "5": "5min",
                "1m": "1min",
                "3m": "3min",
                "5m": "5min",
                "1min": "1min",
                "3min": "3min",
                "5min": "5min",
            }
            return mapping.get(tr, "1min")

        return "1min"

    except Exception:
        return "1min"


# ============================================================
# required column preparation
# ============================================================

def _ensure_required_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    # OHLC alias
    df = _normalize_price_columns(df)

    # symbol
    if "symbol" not in df.columns:
        logger.warning("[summary_indicator] symbol column missing")
        df["symbol"] = ""

    # datetime
    df = _normalize_datetime(df)
    if "datetime" not in df.columns:
        logger.warning("[summary_indicator] datetime column missing -> create NaT")
        df["datetime"] = pd.NaT

    # volume
    if "volume" not in df.columns:
        df["volume"] = 0.0

    # numeric coercion
    df = _to_numeric_if_exists(
        df,
        [
            "open", "high", "low", "close",
            "open_price", "high_price", "low_price", "close_price",
            "volume", "tick_count",
        ],
    )

    return df


# ============================================================
# fallback indicators
# ============================================================

def _safe_groupby_rolling_mean(df: pd.DataFrame, src: str, window: int) -> pd.Series:
    try:
        if "symbol" in df.columns and src in df.columns:
            return (
                pd.to_numeric(df[src], errors="coerce")
                .groupby(df["symbol"])
                .transform(lambda s: s.rolling(window, min_periods=1).mean())
            )
    except Exception:
        logger.exception("[summary_indicator] rolling mean failed src=%s window=%s", src, window)

    return pd.Series([np.nan] * len(df), index=df.index)


def _apply_minimum_fallback_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    add_all_indicators が失敗した場合でも、
    conditions / scoring が最低限動くように基礎列を補う。
    """
    if df.empty:
        return df

    df = df.copy()

    try:
        close_src = "close_price" if "close_price" in df.columns else "close"

        if "ma5" not in df.columns:
            df["ma5"] = _safe_groupby_rolling_mean(df, close_src, 5)

        if "ma25" not in df.columns:
            df["ma25"] = _safe_groupby_rolling_mean(df, close_src, 25)

        if "ma75" not in df.columns:
            df["ma75"] = _safe_groupby_rolling_mean(df, close_src, 75)

        if "vwap" not in df.columns:
            if all(c in df.columns for c in ["close_price", "volume"]):
                try:
                    close_p = pd.to_numeric(df["close_price"], errors="coerce").fillna(0)
                    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

                    if "symbol" in df.columns:
                        cum_pv = (close_p * vol).groupby(df["symbol"]).cumsum()
                        cum_v = vol.groupby(df["symbol"]).cumsum().replace(0, np.nan)
                        df["vwap"] = (cum_pv / cum_v).fillna(close_p)
                    else:
                        cum_pv = (close_p * vol).cumsum()
                        cum_v = vol.cumsum().replace(0, np.nan)
                        df["vwap"] = (cum_pv / cum_v).fillna(close_p)
                except Exception:
                    df["vwap"] = pd.to_numeric(df.get("close_price", 0), errors="coerce").fillna(0)
            else:
                df["vwap"] = pd.to_numeric(df.get(close_src, 0), errors="coerce").fillna(0)

        if "rsi" not in df.columns:
            df["rsi"] = 50.0

        if "macd" not in df.columns:
            df["macd"] = 0.0

        if "signal" not in df.columns:
            df["signal"] = 0.0

        if "bb_upper" not in df.columns:
            base = pd.to_numeric(df.get("ma25", 0), errors="coerce").fillna(0)
            df["bb_upper"] = base

        if "bb_lower" not in df.columns:
            base = pd.to_numeric(df.get("ma25", 0), errors="coerce").fillna(0)
            df["bb_lower"] = base

        if "bb_lower_3sigma" not in df.columns:
            df["bb_lower_3sigma"] = pd.to_numeric(df.get("bb_lower", 0), errors="coerce").fillna(0)

        if "atr" not in df.columns:
            df["atr"] = 0.0

        if "slope_atr_scaled" not in df.columns:
            try:
                close_s = pd.to_numeric(df[close_src], errors="coerce").fillna(0)
                if "symbol" in df.columns:
                    df["slope_atr_scaled"] = close_s.groupby(df["symbol"]).diff().fillna(0)
                else:
                    df["slope_atr_scaled"] = close_s.diff().fillna(0)
            except Exception:
                df["slope_atr_scaled"] = 0.0

    except Exception:
        logger.exception("[summary_indicator] fallback indicators failed")

    return df


# ============================================================
# public api
# ============================================================

def apply_summary_indicators(
    df: Any,
    interval: Optional[int | str] = None
) -> pd.DataFrame:
    """
    旧互換API。
    できるだけ安全に add_all_indicators へ橋渡しする。
    """
    try:
        df = _ensure_dataframe(df)

        if df.empty:
            logger.warning("[summary_indicator] empty input")
            return pd.DataFrame()

        df = _sanitize_inf_nan(df)
        df = _normalize_symbol(df)
        df = _ensure_required_columns(df)
        df = _sort_symbol_datetime(df)

        interval_label = _resolve_interval_label(df, interval)

        required = {
            "symbol",
            "datetime",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
        }

        missing = required - set(df.columns)
        if missing:
            logger.warning(
                "[summary_indicator] required columns missing before add_all_indicators: %s",
                sorted(missing)
            )
            out = _apply_minimum_fallback_indicators(df)
            out = _normalize_price_columns(out)
            out = _sort_symbol_datetime(out)
            return out.reset_index(drop=True)

        try:
            out = add_all_indicators(
                df.copy(),
                interval=interval_label,
            )
        except TypeError:
            # 互換吸収: interval引数非対応版
            out = add_all_indicators(df.copy())
        except Exception:
            logger.exception("[summary_indicator] add_all_indicators failed")
            out = df.copy()

        if not isinstance(out, pd.DataFrame) or out.empty:
            logger.warning("[summary_indicator] add_all_indicators returned empty -> fallback")
            out = df.copy()

        out = _ensure_dataframe(out)
        out = _sanitize_inf_nan(out)
        out = _normalize_price_columns(out)
        out = _normalize_datetime(out)
        out = _normalize_symbol(out)
        out = _apply_minimum_fallback_indicators(out)
        out = _sort_symbol_datetime(out)

        logger.info(
            "[summary_indicator] applied interval=%s rows=%s cols=%s columns=%s",
            interval_label,
            len(out),
            len(out.columns),
            list(out.columns),
        )

        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[summary_indicator] apply_summary_indicators failed")
        try:
            df = _ensure_dataframe(df)
            if df.empty:
                return pd.DataFrame()
            df = _normalize_price_columns(df)
            df = _normalize_datetime(df)
            df = _normalize_symbol(df)
            df = _apply_minimum_fallback_indicators(df)
            df = _sort_symbol_datetime(df)
            return df.reset_index(drop=True)
        except Exception:
            logger.exception("[summary_indicator] final fallback failed")
            return pd.DataFrame()


# ============================================================
# optional compatibility aliases
# ============================================================

def run_summary_indicators(
    df: Any,
    interval: Optional[int | str] = None
) -> pd.DataFrame:
    return apply_summary_indicators(df, interval=interval)


def calculate_summary_indicators(
    df: Any,
    interval: Optional[int | str] = None
) -> pd.DataFrame:
    return apply_summary_indicators(df, interval=interval)