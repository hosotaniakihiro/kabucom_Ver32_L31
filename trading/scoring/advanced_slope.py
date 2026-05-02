# ============================================================
# File   : trading/scoring/advanced_slope.py
# Version: Ver2.0-PRODUCTION-ATR-SLOPE-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ Ver1.2 完全保持（削除ゼロ）
# ✔ ATRスケール化SLOPE
# ✔ 3本差分ベース
# ✔ ローリング平滑化
# ✔ symbol単位完全分離
# ✔ NaN / inf 完全防御
# ✔ close alias 自動吸収（NEW）
# ✔ datetime alias 吸収（NEW）
# ✔ ATRフォールバック（NEW）
# ✔ dtype最終安定化
# ✔ duplicate列完全防御
# ✔ scheduler停止完全禁止
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# numeric guard
# ============================================================

def _safe_numeric(series: pd.Series) -> pd.Series:

    try:

        return (
            pd.to_numeric(series, errors="coerce")
            .replace([np.inf, -np.inf], 0.0)
            .fillna(0.0)
            .astype("float64")
        )

    except Exception:

        return pd.Series(
            np.zeros(len(series)),
            index=series.index,
            dtype="float64"
        )


# ============================================================
# alias repair
# ============================================================

def _ensure_close(df: pd.DataFrame) -> pd.DataFrame:

    if "close" not in df.columns:

        if "close_price" in df.columns:
            df["close"] = df["close_price"]

    return df


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" not in df.columns:

        for alt in ("end_time", "time", "timestamp", "snapshot_time"):

            if alt in df.columns:

                df["datetime"] = df[alt]
                break

    return df


def _ensure_atr(df: pd.DataFrame, atr_col: str) -> pd.DataFrame:

    if atr_col not in df.columns:

        logger.warning(
            "[advanced_slope] ATR missing -> fallback constant"
        )

        df[atr_col] = 1.0

    return df


# ============================================================
# main slope engine
# ============================================================

def apply_atr_scaled_slope(
    df: pd.DataFrame,
    *,
    slope_period: int = 3,
    smooth_period: int = 3,
    atr_col: str = "atr_1m",
    clip_value: float = 10.0,
) -> pd.DataFrame:

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # duplicate columns guard
        # ----------------------------------------------------

        if df.columns.duplicated().any():

            dup = list(df.columns[df.columns.duplicated()])

            logger.warning(
                "[advanced_slope] duplicate columns removed: %s",
                dup
            )

            df = df.loc[:, ~df.columns.duplicated()]

        # ----------------------------------------------------
        # alias repair
        # ----------------------------------------------------

        df = _ensure_close(df)
        df = _ensure_datetime(df)
        df = _ensure_atr(df, atr_col)

        # ----------------------------------------------------
        # required check
        # ----------------------------------------------------

        required = {"symbol", "close", atr_col}

        if not required.issubset(df.columns):

            logger.warning(
                "[advanced_slope] missing columns: %s",
                required - set(df.columns)
            )

            return df

        # ----------------------------------------------------
        # dtype stabilization
        # ----------------------------------------------------

        df["symbol"] = df["symbol"].astype(str)

        df["close"] = _safe_numeric(df["close"])
        df[atr_col] = _safe_numeric(df[atr_col])

        # ATRゼロ防止
        df.loc[df[atr_col] <= 0, atr_col] = np.nan

        # ----------------------------------------------------
        # sorting
        # ----------------------------------------------------

        if "datetime" in df.columns:

            df["datetime"] = pd.to_datetime(
                df["datetime"],
                errors="coerce"
            )

            df = df.sort_values(["symbol", "datetime"])

        else:

            df = df.sort_values(["symbol"])

        # ----------------------------------------------------
        # slope raw
        # ----------------------------------------------------

        df["slope_raw"] = (
            df.groupby("symbol")["close"]
            .diff(slope_period)
        )

        df["slope_raw"] = _safe_numeric(df["slope_raw"])

        # ----------------------------------------------------
        # ATR scale
        # ----------------------------------------------------

        df["slope_atr_scaled"] = (
            df["slope_raw"] / df[atr_col]
        )

        df["slope_atr_scaled"] = (
            df["slope_atr_scaled"]
            .replace([np.inf, -np.inf], 0.0)
            .fillna(0.0)
        )

        # ----------------------------------------------------
        # smoothing
        # ----------------------------------------------------

        if smooth_period > 1:

            df["slope_atr_scaled"] = (
                df.groupby("symbol")["slope_atr_scaled"]
                .rolling(
                    window=smooth_period,
                    min_periods=1
                )
                .mean()
                .reset_index(level=0, drop=True)
            )

        # ----------------------------------------------------
        # clip
        # ----------------------------------------------------

        df["slope_atr_scaled"] = df["slope_atr_scaled"].clip(
            -clip_value,
            clip_value
        )

        # ----------------------------------------------------
        # dtype finalize
        # ----------------------------------------------------

        df["slope_raw"] = df["slope_raw"].astype("float64")
        df["slope_atr_scaled"] = df["slope_atr_scaled"].astype("float64")

        return df

    except Exception:

        logger.exception("[advanced_slope] fatal error")

        return df


# ============================================================
# backward compatibility
# ============================================================

def apply_advanced_slope(df: pd.DataFrame) -> pd.DataFrame:

    return apply_atr_scaled_slope(df)