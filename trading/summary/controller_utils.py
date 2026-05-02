# ==========================================================
# File   : trading/summary/controller_utils.py
# Version: Ver1.0-PRODUCTION-HARDENED-CONTROLLER-UTILS
# ----------------------------------------------------------
# 役割:
#   - DataFrame 正規化
#   - symbol / datetime 整形
#   - OHLC / price alias 整形
#   - 汎用 safe helper
#
# 分離元:
#   - trading/summary/summary_controller.py
# ==========================================================

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ==========================================================
# basic dataframe sanitizers
# ==========================================================

def sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            try:
                df = pd.DataFrame(df)
            except Exception:
                return pd.DataFrame()

        if df.empty:
            return df.copy()

        out = df.copy()
        out = out.replace([np.inf, -np.inf], np.nan)

        try:
            if isinstance(out.columns, pd.MultiIndex):
                out.columns = [
                    "_".join([str(x) for x in col if str(x) != ""]).strip("_")
                    if isinstance(col, tuple) else str(col)
                    for col in out.columns
                ]
        except Exception:
            logger.debug("[summary_controller] multiindex flatten failed", exc_info=True)

        try:
            if out.columns.duplicated().any():
                dup = out.columns[out.columns.duplicated()].tolist()
                logger.warning("[summary_controller] duplicate columns removed: %s", dup)
                out = out.loc[:, ~out.columns.duplicated(keep="last")]
        except Exception:
            logger.debug("[summary_controller] duplicate column cleanup failed", exc_info=True)

        return out

    except Exception:
        logger.exception("[summary_controller] sanitize failed")
        return pd.DataFrame()


def ensure_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = sanitize_df(df)
    if out.empty:
        return out

    if "symbol" not in out.columns:
        for c in ("Symbol", "code", "Code", "ticker", "stock_code", "銘柄コード"):
            if c in out.columns:
                try:
                    out["symbol"] = out[c]
                    break
                except Exception:
                    pass

    if "symbol" not in out.columns:
        return out

    try:
        out["symbol"] = (
            out["symbol"]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
        )
        out = out[
            out["symbol"].notna()
            & (out["symbol"] != "")
            & (out["symbol"].str.lower() != "nan")
            & (out["symbol"].str.lower() != "none")
        ].copy()
    except Exception:
        logger.debug("[summary_controller] ensure symbol failed", exc_info=True)

    return out


def ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = sanitize_df(df)
    if out.empty:
        return out

    try:
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        elif "date" in out.columns and "start_time" in out.columns:
            out["datetime"] = pd.to_datetime(
                out["date"].astype(str).str.strip() + " " + out["start_time"].astype(str).str.strip(),
                errors="coerce",
            )
        elif "date" in out.columns and "time" in out.columns:
            out["datetime"] = pd.to_datetime(
                out["date"].astype(str).str.strip() + " " + out["time"].astype(str).str.strip(),
                errors="coerce",
            )
        elif "end_time" in out.columns and "date" in out.columns:
            out["datetime"] = pd.to_datetime(
                out["date"].astype(str).str.strip() + " " + out["end_time"].astype(str).str.strip(),
                errors="coerce",
            )
        else:
            out["datetime"] = pd.NaT

        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
    except Exception:
        logger.debug("[summary_controller] ensure datetime failed", exc_info=True)

    return out


# ==========================================================
# numeric alias helpers
# ==========================================================

def coalesce_first_numeric(df: pd.DataFrame, dest: str, candidates: tuple[str, ...]) -> pd.DataFrame:
    out = sanitize_df(df)
    if out.empty:
        return out

    if dest not in out.columns:
        out[dest] = np.nan

    try:
        base = pd.to_numeric(out[dest], errors="coerce")
    except Exception:
        base = pd.Series(np.nan, index=out.index)

    for c in candidates:
        if c not in out.columns:
            continue
        try:
            s = pd.to_numeric(out[c], errors="coerce")
            try:
                base = base.combine_first(s)
            except Exception:
                base = base.where(base.notna(), s)
        except Exception:
            logger.debug("[summary_controller] coalesce failed dest=%s src=%s", dest, c, exc_info=True)

    out[dest] = base
    return out


def normalize_price_aliases(df: pd.DataFrame) -> pd.DataFrame:
    out = sanitize_df(df)
    if out.empty:
        return out

    out = coalesce_first_numeric(
        out,
        "close",
        ("close", "close_price", "price", "current_price", "CurrentPrice", "last_price", "LastPrice"),
    )
    out = coalesce_first_numeric(
        out,
        "open",
        ("open", "open_price", "OpeningPrice", "opening_price", "close"),
    )
    out = coalesce_first_numeric(
        out,
        "high",
        ("high", "high_price", "close"),
    )
    out = coalesce_first_numeric(
        out,
        "low",
        ("low", "low_price", "close"),
    )
    out = coalesce_first_numeric(
        out,
        "volume",
        ("volume", "Volume", "trading_volume", "TradingVolume"),
    )

    try:
        close_num = pd.to_numeric(out["close"], errors="coerce")
        for c in ("open", "high", "low"):
            s = pd.to_numeric(out[c], errors="coerce")
            try:
                out[c] = s.combine_first(close_num)
            except Exception:
                out[c] = s.where(s.notna(), close_num)
    except Exception:
        logger.debug("[summary_controller] ohlc backfill from close failed", exc_info=True)

    return out


# ==========================================================
# public normalization entry
# ==========================================================

def normalize_summary_df(df: pd.DataFrame) -> pd.DataFrame:
    out = sanitize_df(df)
    if out.empty:
        return out

    out = ensure_symbol(out)
    out = ensure_datetime(out)
    out = normalize_price_aliases(out)

    try:
        if "symbolname" not in out.columns:
            if "name" in out.columns:
                out["symbolname"] = out["name"].astype(str)
            else:
                out["symbolname"] = ""
    except Exception:
        out["symbolname"] = ""

    try:
        out = out.dropna(subset=["symbol", "datetime"]).copy()
    except Exception:
        pass

    try:
        out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
    except Exception:
        logger.debug("[summary_controller] normalize sort failed", exc_info=True)

    return out


# ==========================================================
# safe numeric/meta helpers
# ==========================================================

def safe_len(df) -> int:
    try:
        return 0 if df is None else int(len(df))
    except Exception:
        return 0


def safe_symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def safe_latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "end_time", "snapshot_time", "tick_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    return s.max()
        return None
    except Exception:
        return None


def safe_numeric_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        best = 0
        for c in cols:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce").fillna(0)
                best = max(best, int((s != 0).sum()))
        return best
    except Exception:
        return 0


def safe_numeric_nonnull(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        best = 0
        for c in cols:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                best = max(best, int(s.notna().sum()))
        return best
    except Exception:
        return 0


def safe_ready_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "technical_ready" not in df.columns:
            return 0
        s = pd.Series(df["technical_ready"]).fillna(False).astype(bool)
        return int(s.sum())
    except Exception:
        return 0


def safe_ready_symbol_count(df: pd.DataFrame) -> int:
    try:
        if (
            not isinstance(df, pd.DataFrame)
            or df.empty
            or "symbol" not in df.columns
            or "technical_ready" not in df.columns
        ):
            return 0
        ready = pd.Series(df["technical_ready"]).fillna(False).astype(bool)
        return int(df.loc[ready, "symbol"].astype(str).nunique())
    except Exception:
        return 0


def profile_numeric_series(df: pd.DataFrame, col: str) -> str:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return f"{col}=MISSING"

        if col == "source":
            vc = df[col].fillna("NULL").astype(str).value_counts(dropna=False).to_dict()
            return f"{col}={vc}"

        if col == "technical_ready":
            s = pd.Series(df[col]).fillna(False).astype(bool)
            return (
                f"{col}: non_null={int(s.notna().sum())} "
                f"nonzero={int(s.sum())} "
                f"nunique={int(pd.Series(s).nunique(dropna=True))} "
                f"min={s.min()} max={s.max()}"
            )

        s = pd.to_numeric(df[col], errors="coerce")
        return (
            f"{col}: non_null={int(s.notna().sum())} "
            f"nonzero={int((s.fillna(0) != 0).sum())} "
            f"eq_2000={int((s.fillna(0) == 2000).sum())} "
            f"eq_-2000={int((s.fillna(0) == -2000).sum())} "
            f"nunique={int(s.nunique(dropna=True))} "
            f"min={s.min()} max={s.max()}"
        )
    except Exception:
        return f"{col}=PROFILE_FAILED"


__all__ = [
    "sanitize_df",
    "ensure_symbol",
    "ensure_datetime",
    "coalesce_first_numeric",
    "normalize_price_aliases",
    "normalize_summary_df",
    "safe_len",
    "safe_symbol_count",
    "safe_latest_dt",
    "safe_numeric_nonzero",
    "safe_numeric_nonnull",
    "safe_ready_count",
    "safe_ready_symbol_count",
    "profile_numeric_series",
]