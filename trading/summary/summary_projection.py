# ==========================================================
# File   : trading/summary/controller_projection.py
# Version: Ver1.0-PRODUCTION-HARDENED-CONTROLLER-PROJECTION
# ----------------------------------------------------------
# 役割:
#   - latest projection
#   - history length attach
#   - technical_ready rebuild
#   - mature-first selection
#   - summary controller diagnostics
#
# 分離元:
#   - trading/summary/summary_controller.py
# ==========================================================

from __future__ import annotations

import logging
from typing import Callable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MIN_HISTORY_ROWS_RSI = 14
MIN_HISTORY_ROWS_MACD = 26
MIN_HISTORY_ROWS_MA75 = 75
MIN_HISTORY_ROWS_STRONG = 80


# ==========================================================
# small safe helpers
# ==========================================================

def _safe_len(df) -> int:
    try:
        return 0 if df is None else int(len(df))
    except Exception:
        return 0


def _safe_symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _safe_latest_dt(df: pd.DataFrame):
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


def _safe_numeric_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
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


def _safe_numeric_nonnull(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
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


def _safe_ready_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "technical_ready" not in df.columns:
            return 0
        s = pd.Series(df["technical_ready"]).fillna(False).astype(bool)
        return int(s.sum())
    except Exception:
        return 0


def _safe_ready_symbol_count(df: pd.DataFrame) -> int:
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


def _profile_numeric_series(df: pd.DataFrame, col: str) -> str:
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


# ==========================================================
# diagnostics
# ==========================================================

def log_df_state(
    label: str,
    interval: int,
    df: pd.DataFrame,
) -> None:
    try:
        rows = _safe_len(df)
        cols = 0 if not isinstance(df, pd.DataFrame) else len(df.columns)
        symbols = _safe_symbol_count(df)
        latest_dt = _safe_latest_dt(df)

        logger.info(
            "[summary_controller] %s interval=%s rows=%s cols=%s symbols=%s latest_dt=%s",
            label,
            interval,
            rows,
            cols,
            symbols,
            latest_dt,
        )

        if isinstance(df, pd.DataFrame) and not df.empty:
            for c in (
                "score",
                "score_buy",
                "score_sell",
                "slope_atr_scaled",
                "mtf",
                "rsi",
                "macd",
                "close",
                "close_price",
                "volume",
                "technical_ready",
                "source",
                "symbol_hist_len",
            ):
                if c in df.columns:
                    logger.info(
                        "[summary_controller] %s interval=%s %s",
                        label,
                        interval,
                        _profile_numeric_series(df, c),
                    )

            logger.info(
                "[summary_controller] %s interval=%s ready_rows=%s ready_symbols=%s",
                label,
                interval,
                _safe_ready_count(df),
                _safe_ready_symbol_count(df),
            )

    except Exception:
        logger.exception("[summary_controller] df state log failed label=%s interval=%s", label, interval)


def log_scoring_probe(
    label: str,
    interval: int,
    df: pd.DataFrame,
) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.info("[summary_controller] %s interval=%s empty", label, interval)
            return

        logger.info(
            "[summary_controller] %s interval=%s rows=%s cols=%s symbols=%s latest_dt=%s",
            label,
            interval,
            len(df),
            len(df.columns),
            _safe_symbol_count(df),
            _safe_latest_dt(df),
        )

        probe_cols = [
            "score", "score_buy", "score_sell", "buy_score", "sell_score",
            "ranking_score", "slope", "slope_atr_scaled", "mtf", "score_mtf",
            "mtf_score", "mtf_alignment", "rsi", "macd", "close", "close_price",
            "price", "volume", "technical_ready", "symbol_hist_len",
        ]

        for c in probe_cols:
            logger.info(
                "[summary_controller] %s interval=%s %s",
                label,
                interval,
                _profile_numeric_series(df, c),
            )

        logger.info(
            "[summary_controller] %s interval=%s ready_rows=%s ready_symbols=%s",
            label,
            interval,
            _safe_ready_count(df),
            _safe_ready_symbol_count(df),
        )

    except Exception:
        logger.exception("[summary_controller] scoring probe failed label=%s interval=%s", label, interval)


def log_history_density(
    label: str,
    interval: int,
    df: pd.DataFrame,
) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns or "datetime" not in df.columns:
            logger.info("[summary_controller] %s interval=%s empty_or_missing_keys", label, interval)
            return

        out = df.copy()
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["symbol", "datetime"]).copy()

        vc = out.groupby("symbol")["datetime"].count().sort_values(ascending=False)
        if vc.empty:
            logger.info("[summary_controller] %s interval=%s no_history_counts", label, interval)
            return

        logger.info(
            "[summary_controller] %s interval=%s symbols=%s rows=%s hist_len[min=%s p25=%.2f med=%.2f p75=%.2f max=%s mean=%.2f]",
            label,
            interval,
            int(vc.shape[0]),
            int(len(out)),
            int(vc.min()),
            float(vc.quantile(0.25)),
            float(vc.quantile(0.50)),
            float(vc.quantile(0.75)),
            int(vc.max()),
            float(vc.mean()),
        )
    except Exception:
        logger.exception("[summary_controller] history density log failed label=%s interval=%s", label, interval)


# ==========================================================
# history len
# ==========================================================

def history_len_per_symbol(df: pd.DataFrame) -> pd.Series:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return pd.Series(dtype="int64")
        return df.groupby("symbol")["symbol"].count().astype("int64")
    except Exception:
        return pd.Series(dtype="int64")


def attach_history_len(
    latest_df: pd.DataFrame,
    hist_df: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    out = normalize_fn(latest_df)
    if out.empty or "symbol" not in out.columns:
        return out

    try:
        vc = history_len_per_symbol(hist_df)
        out["symbol_hist_len"] = out["symbol"].map(vc).fillna(0).astype(int)
        return out
    except Exception:
        logger.exception("[summary_controller] attach history len failed")
        out["symbol_hist_len"] = 0
        return out


# ==========================================================
# technical ready rebuild
# ==========================================================

def rebuild_technical_ready(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out

    if "technical_ready" not in out.columns:
        out["technical_ready"] = False

    try:
        hist_len = (
            pd.to_numeric(out["symbol_hist_len"], errors="coerce").fillna(0)
            if "symbol_hist_len" in out.columns
            else pd.Series(0, index=out.index)
        )
        rsi_ok = (
            pd.to_numeric(out["rsi"], errors="coerce").notna()
            if "rsi" in out.columns
            else pd.Series(False, index=out.index)
        )
        macd_ok = (
            pd.to_numeric(out["macd"], errors="coerce").notna()
            if "macd" in out.columns
            else pd.Series(False, index=out.index)
        )
        slope_ok = (
            pd.to_numeric(out["slope"], errors="coerce").fillna(0).ne(0)
            if "slope" in out.columns
            else pd.Series(False, index=out.index)
        )
        mtf_ok = (
            pd.to_numeric(out["mtf"], errors="coerce").fillna(0).ne(0)
            if "mtf" in out.columns
            else pd.Series(False, index=out.index)
        )
        ma75_ok = (
            pd.to_numeric(out["ma75"], errors="coerce").notna()
            if "ma75" in out.columns
            else pd.Series(False, index=out.index)
        )

        ready_strong = (
            (hist_len >= MIN_HISTORY_ROWS_MACD)
            & (macd_ok | rsi_ok | slope_ok | mtf_ok | ma75_ok)
        )

        price_ok = (
            pd.to_numeric(out["close"], errors="coerce").notna()
            if "close" in out.columns
            else pd.Series(False, index=out.index)
        )
        volume_ok = (
            pd.to_numeric(out["volume"], errors="coerce").fillna(0).gt(0)
            if "volume" in out.columns
            else pd.Series(False, index=out.index)
        )

        ready_soft = (
            (hist_len >= MIN_HISTORY_ROWS_RSI)
            & price_ok
            & (rsi_ok | slope_ok | ma75_ok | volume_ok)
        )

        ready = ready_strong | ready_soft
        out["technical_ready"] = ready.fillna(False).astype(bool)

    except Exception:
        logger.exception("[summary_controller] rebuild technical_ready failed")

    return out


# ==========================================================
# source priority
# ==========================================================

def source_priority_series(df: pd.DataFrame) -> pd.Series:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series(dtype="int64")

    if "source" not in df.columns:
        return pd.Series(0, index=df.index, dtype="int64")

    try:
        src = df["source"].fillna("").astype(str)
        out = pd.Series(0, index=df.index, dtype="int64")

        out.loc[src.str.startswith("summary_recovery_push_1m", na=False)] = 500
        out.loc[src.str.startswith("summary_recovery_resample_1m", na=False)] = 450
        out.loc[src.str.startswith("summary_recovery_", na=False)] = 400
        out.loc[src.eq("SUMMARY")] = 350
        out.loc[src.str.startswith("ranking_history_", na=False)] = 120
        out.loc[src.str.startswith("RANKING_", na=False)] = 100
        return out
    except Exception:
        logger.exception("[summary_controller] source priority build failed")
        return pd.Series(0, index=df.index, dtype="int64")


# ==========================================================
# latest projection
# ==========================================================

def latest_row_per_symbol(
    df: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    out = normalize_fn(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return out

    try:
        out = (
            out.sort_values(["symbol", "datetime"], kind="mergesort")
               .drop_duplicates(["symbol"], keep="last")
               .reset_index(drop=True)
        )
        return out
    except Exception:
        logger.exception("[summary_controller] latest row per symbol failed")
        return out


def latest_row_per_symbol_mature_first(
    df: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    out = normalize_fn(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return out

    try:
        out = out.copy()

        out["_ready_rank"] = (
            pd.Series(out["technical_ready"]).fillna(False).astype(bool).astype(int)
            if "technical_ready" in out.columns else 0
        )
        out["_hist_rank"] = (
            pd.to_numeric(out["symbol_hist_len"], errors="coerce").fillna(0)
            if "symbol_hist_len" in out.columns else 0
        )
        out["_source_rank"] = source_priority_series(out)
        out["_rsi_rank"] = (
            pd.to_numeric(out["rsi"], errors="coerce").notna().astype(int)
            if "rsi" in out.columns else 0
        )
        out["_macd_rank"] = (
            pd.to_numeric(out["macd"], errors="coerce").notna().astype(int)
            if "macd" in out.columns else 0
        )
        out["_slope_rank"] = (
            pd.to_numeric(out["slope"], errors="coerce").fillna(0).ne(0).astype(int)
            if "slope" in out.columns else 0
        )

        out = (
            out.sort_values(
                ["symbol", "_ready_rank", "_hist_rank", "_source_rank", "_rsi_rank", "_macd_rank", "_slope_rank", "datetime"],
                ascending=[True, False, False, False, False, False, False, False],
                kind="mergesort",
            )
            .drop_duplicates(["symbol"], keep="first")
            .reset_index(drop=True)
        )

        logger.info(
            "[summary_controller] mature-first latest projection rows=%s symbols=%s ready_rows=%s ready_symbols=%s",
            len(out),
            _safe_symbol_count(out),
            _safe_ready_count(out),
            _safe_ready_symbol_count(out),
        )

        return out.drop(
            columns=["_ready_rank", "_hist_rank", "_source_rank", "_rsi_rank", "_macd_rank", "_slope_rank"],
            errors="ignore",
        )

    except Exception:
        logger.exception("[summary_controller] mature-first latest projection failed")
        return latest_row_per_symbol(df, normalize_fn)


__all__ = [
    "MIN_HISTORY_ROWS_RSI",
    "MIN_HISTORY_ROWS_MACD",
    "MIN_HISTORY_ROWS_MA75",
    "MIN_HISTORY_ROWS_STRONG",
    "log_df_state",
    "log_scoring_probe",
    "log_history_density",
    "history_len_per_symbol",
    "attach_history_len",
    "rebuild_technical_ready",
    "source_priority_series",
    "latest_row_per_symbol",
    "latest_row_per_symbol_mature_first",
]