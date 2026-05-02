# ============================================================
# File   : trading/scoring/features/feature_engine.py
# Version: Ver2.1-PRODUCTION-ULTRA-STABLE-FEATURE-ENGINE
# ------------------------------------------------------------
# ✔ Ver2.0 完全保持（削除ゼロ）
# ✔ ATR auto generation
# ✔ slope auto generation
# ✔ slope_atr_scaled generation
# ✔ mtf generation
# ✔ price alias repair
# ✔ symbol safe calculation
# ✔ pandas alignment safety
# ✔ dtype stabilization
# ✔ NaN / inf guard
# ✔ duplicate column guard
# ✔ MultiIndex flatten
# ✔ Series/DataFrame column repair
# ✔ index stabilization
# ✔ production stability
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# dataframe repair
# ============================================================

def _repair_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:

        # flatten MultiIndex columns
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [
                "_".join(map(str, c)) for c in df.columns
            ]

        # remove duplicate columns
        if df.columns.duplicated().any():
            dup = list(df.columns[df.columns.duplicated()])
            logger.warning(
                "[FEATURE ENGINE] duplicate columns removed: %s",
                dup
            )
            df = df.loc[:, ~df.columns.duplicated()].copy()

        # index stabilization
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index(drop=True)

        if df.index.duplicated().any():
            df = df.reset_index(drop=True)

        if not isinstance(df.index, pd.RangeIndex):
            df = df.reset_index(drop=True)

    except Exception:
        logger.exception(
            "[FEATURE ENGINE] dataframe repair failed"
        )

    return df


# ============================================================
# safe series extractor
# ============================================================

def _safe_series(df: pd.DataFrame, col: str):

    if col not in df.columns:
        return pd.Series(0.0, index=df.index)

    s = df[col]

    try:

        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]

        s = pd.to_numeric(s, errors="coerce")

        s = s.replace([np.inf, -np.inf], np.nan)

        return s.fillna(0)

    except Exception:

        return pd.Series(0.0, index=df.index)


# ============================================================
# price alias repair
# ============================================================

def _repair_price_alias(df: pd.DataFrame) -> pd.DataFrame:

    alias_map = {
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "open_price": "open",
    }

    try:

        for src, dst in alias_map.items():

            if dst not in df.columns and src in df.columns:
                df[dst] = df[src]

    except Exception:
        pass

    return df


# ============================================================
# ATR generation
# ============================================================

def _ensure_atr(df: pd.DataFrame) -> pd.DataFrame:

    if "atr_1m" in df.columns:
        return df

    required = {"high", "low", "close"}

    if not required.issubset(df.columns):
        return df

    try:

        h = _safe_series(df, "high")
        l = _safe_series(df, "low")
        c = _safe_series(df, "close")

        if "symbol" in df.columns:

            def calc(group):

                h = _safe_series(group, "high")
                l = _safe_series(group, "low")
                c = _safe_series(group, "close")

                tr = np.maximum(
                    h - l,
                    np.maximum(
                        abs(h - c.shift()),
                        abs(l - c.shift())
                    )
                )

                group["atr_1m"] = (
                    tr.rolling(14, min_periods=1).mean()
                )

                return group

            df = df.groupby(
                "symbol",
                group_keys=False,
                sort=False
            ).apply(calc)

        else:

            tr = np.maximum(
                h - l,
                np.maximum(
                    abs(h - c.shift()),
                    abs(l - c.shift())
                )
            )

            df["atr_1m"] = (
                pd.Series(tr)
                .rolling(14, min_periods=1)
                .mean()
            )

    except Exception:

        logger.exception(
            "[FEATURE ENGINE] ATR generation failed"
        )

        df["atr_1m"] = 0.0

    return df


# ============================================================
# slope generation
# ============================================================

def _ensure_slope(df: pd.DataFrame) -> pd.DataFrame:

    if "slope" in df.columns:
        return df

    if "close" not in df.columns:
        return df

    try:

        close = _safe_series(df, "close")

        if "symbol" in df.columns:

            df["slope"] = (
                close.groupby(df["symbol"])
                .diff()
                .fillna(0)
            )

        else:

            df["slope"] = close.diff().fillna(0)

    except Exception:

        logger.exception(
            "[FEATURE ENGINE] slope generation failed"
        )

        df["slope"] = 0.0

    return df


# ============================================================
# slope_atr_scaled
# ============================================================

def _ensure_slope_atr_scaled(df: pd.DataFrame) -> pd.DataFrame:

    if "slope_atr_scaled" in df.columns:
        return df

    if "slope" not in df.columns:
        return df

    if "atr_1m" not in df.columns:
        return df

    try:

        slope = _safe_series(df, "slope")
        atr = _safe_series(df, "atr_1m").replace(0, np.nan)

        df["slope_atr_scaled"] = slope / (atr + 1e-9)

        df["slope_atr_scaled"] = (
            df["slope_atr_scaled"]
            .replace([np.inf, -np.inf], 0)
            .fillna(0)
        )

    except Exception:

        logger.exception(
            "[FEATURE ENGINE] slope_atr_scaled failed"
        )

        df["slope_atr_scaled"] = 0.0

    return df


# ============================================================
# mtf generation
# ============================================================

def _ensure_mtf(df: pd.DataFrame) -> pd.DataFrame:

    if "mtf" in df.columns:
        return df

    try:

        if "slope_atr_scaled" in df.columns:

            df["mtf"] = _safe_series(df, "slope_atr_scaled")

        elif "slope" in df.columns:

            df["mtf"] = _safe_series(df, "slope")

        else:

            df["mtf"] = 0.0

    except Exception:

        logger.exception(
            "[FEATURE ENGINE] mtf generation failed"
        )

        df["mtf"] = 0.0

    return df


# ============================================================
# ensure feature columns
# ============================================================

def _ensure_feature_columns(df: pd.DataFrame) -> pd.DataFrame:

    required = [
        "atr_1m",
        "slope",
        "slope_atr_scaled",
        "mtf",
    ]

    try:

        for col in required:

            if col not in df.columns:
                df[col] = 0.0

    except Exception:
        pass

    return df


# ============================================================
# dtype stabilization
# ============================================================

def _stabilize_feature_dtypes(df: pd.DataFrame) -> pd.DataFrame:

    cols = [
        "atr_1m",
        "slope",
        "slope_atr_scaled",
        "mtf",
    ]

    try:

        for c in cols:

            if c in df.columns:

                df[c] = pd.to_numeric(
                    df[c],
                    errors="coerce"
                ).fillna(0)

    except Exception:
        pass

    return df


# ============================================================
# feature engine
# ============================================================

def ensure_features(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        df = _repair_dataframe(df)

        df = _repair_price_alias(df)

        df = _ensure_atr(df)

        df = _ensure_slope(df)

        df = _ensure_slope_atr_scaled(df)

        df = _ensure_mtf(df)

        df = _ensure_feature_columns(df)

        df = _stabilize_feature_dtypes(df)

        return df

    except Exception:

        logger.exception(
            "[FEATURE ENGINE] feature generation failed"
        )

        return df


# ============================================================
# pipeline compatibility wrapper
# ============================================================

def generate_features(df: pd.DataFrame) -> pd.DataFrame:

    try:
        return ensure_features(df)

    except Exception:

        logger.exception(
            "[FEATURE ENGINE] generate_features failed"
        )

        return df