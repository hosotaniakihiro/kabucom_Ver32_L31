# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/indicators_scoring.py
# Version: PRODUCTION-STABLE-REV1.0-INDICATORS-SCORING
# ------------------------------------------------------------
# 【概要】
#   indicator / scoring / score aliases / ready flags 適用
# ============================================================

from __future__ import annotations

import importlib
import logging

import numpy as np
import pandas as pd

from .dataframe_utils import (
    ensure_df,
    normalize_summary_df,
    attach_date_time_columns,
)
from .datetime_guard import drop_future_datetime_rows
from .ready_flags import (
    mask_unready_zero_indicators_to_nan,
    attach_ready_flags,
)

logger = logging.getLogger(__name__)


def apply_indicators(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = ensure_df(df)
    if out.empty:
        return out

    out = drop_future_datetime_rows(out, interval=int(interval), label="before_indicators")
    if out.empty:
        return out

    interval_label = f"{int(interval)}min"

    try:
        from trading.summary.indicators.indicator_calculator import add_all_indicators

        out = add_all_indicators(out, interval=interval_label)
        out = ensure_df(out)
        out = drop_future_datetime_rows(out, interval=int(interval), label="after_indicators")

        logger.info(
            "[MTF HISTORY BOOTSTRAP] indicators applied interval=%s rows=%s symbols=%s",
            interval_label,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns and not out.empty else 0,
        )

    except Exception:
        logger.warning(
            "[MTF HISTORY BOOTSTRAP] add_all_indicators unavailable/failed interval=%s -> continue without indicators",
            interval_label,
            exc_info=True,
        )

    return out


def resolve_scoring_callable():
    candidates = [
        ("trading.scoring.core.scoring_core", "scoring_main"),
        ("trading.scoring.core.scoring_pipeline", "run_scoring_pipeline"),
        ("trading.scoring.core.scoring_pipeline", "apply_scoring_pipeline"),
        ("trading.scoring.core.scoring_core", "run_scoring"),
        ("trading.scoring.core.scoring_core", "apply_scoring"),
    ]

    for mod_name, fn_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                return fn, f"{mod_name}.{fn_name}"
        except Exception:
            logger.debug("[MTF HISTORY BOOTSTRAP] scoring resolve failed %s.%s", mod_name, fn_name, exc_info=True)

    return None, None


def apply_scoring(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = ensure_df(df)
    if out.empty:
        return out

    out = drop_future_datetime_rows(out, interval=int(interval), label="before_scoring")
    if out.empty:
        return out

    interval_label = f"{int(interval)}min"
    fn, name = resolve_scoring_callable()

    if not callable(fn):
        logger.warning("[MTF HISTORY BOOTSTRAP] scoring backend unavailable interval=%s", interval_label)
        return out

    try:
        try:
            scored = fn(out, interval=interval_label, force=True)
        except TypeError:
            try:
                scored = fn(out, interval=interval_label)
            except TypeError:
                try:
                    scored = fn(out, interval_label)
                except TypeError:
                    scored = fn(out)

        scored = ensure_df(scored)
        if scored.empty:
            logger.warning("[MTF HISTORY BOOTSTRAP] scoring returned empty backend=%s interval=%s", name, interval_label)
            return out

        scored = drop_future_datetime_rows(scored, interval=int(interval), label="after_scoring")
        if scored.empty:
            logger.warning("[MTF HISTORY BOOTSTRAP] scoring returned no valid datetime backend=%s interval=%s", name, interval_label)
            return out

        logger.info(
            "[MTF HISTORY BOOTSTRAP] scoring applied backend=%s interval=%s rows=%s symbols=%s",
            name,
            interval_label,
            len(scored),
            scored["symbol"].nunique() if "symbol" in scored.columns and not scored.empty else 0,
        )

        return scored

    except Exception:
        logger.warning(
            "[MTF HISTORY BOOTSTRAP] scoring failed backend=%s interval=%s -> keep indicators",
            name,
            interval_label,
            exc_info=True,
        )
        return out


def ensure_score_aliases(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_df(df)
    if out.empty:
        return out

    try:
        buy = pd.Series(np.nan, index=out.index, dtype="float64")
        sell = pd.Series(np.nan, index=out.index, dtype="float64")

        for c in ("score_buy", "buy_score", "buy"):
            if c in out.columns:
                buy = buy.combine_first(pd.to_numeric(out[c], errors="coerce"))

        for c in ("score_sell", "sell_score", "sell"):
            if c in out.columns:
                sell = sell.combine_first(pd.to_numeric(out[c], errors="coerce"))

        out["score_buy"] = buy
        out["score_sell"] = sell

        if "buy_score" not in out.columns:
            out["buy_score"] = out["score_buy"]

        if "sell_score" not in out.columns:
            out["sell_score"] = out["score_sell"]

        fallback = buy.combine_first(sell)

        try:
            both = buy.notna() & sell.notna()
            choose_sell = sell.abs() > buy.abs()
            fallback.loc[both & choose_sell] = sell.loc[both & choose_sell]
            fallback.loc[both & ~choose_sell] = buy.loc[both & ~choose_sell]
        except Exception:
            pass

        if "score" not in out.columns:
            out["score"] = fallback
        else:
            out["score"] = pd.to_numeric(out["score"], errors="coerce").combine_first(fallback)

        for c in ("score_total", "display_score", "final_score"):
            if c not in out.columns:
                out[c] = pd.to_numeric(out["score"], errors="coerce")
            else:
                out[c] = pd.to_numeric(out[c], errors="coerce").combine_first(
                    pd.to_numeric(out["score"], errors="coerce")
                )

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] ensure score aliases failed", exc_info=True)

    return out


def apply_indicators_scoring_ready(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = normalize_summary_df(df)
    if out.empty:
        return out

    out = attach_date_time_columns(out, interval=int(interval))
    out = drop_future_datetime_rows(out, interval=int(interval), label="apply_start")

    try:
        out["symbol_hist_len"] = out.groupby("symbol")["datetime"].transform("nunique").astype(int)
    except Exception:
        out["symbol_hist_len"] = 0

    out = apply_indicators(out, interval=int(interval))
    out = attach_date_time_columns(out, interval=int(interval))
    out = drop_future_datetime_rows(out, interval=int(interval), label="apply_after_indicators")

    try:
        out["symbol_hist_len"] = out.groupby("symbol")["datetime"].transform("nunique").astype(int)
    except Exception:
        pass

    out = apply_scoring(out, interval=int(interval))
    out = attach_date_time_columns(out, interval=int(interval))
    out = drop_future_datetime_rows(out, interval=int(interval), label="apply_after_scoring")

    out = ensure_score_aliases(out)

    try:
        out["symbol_hist_len"] = out.groupby("symbol")["datetime"].transform("nunique").astype(int)
    except Exception:
        pass

    out = mask_unready_zero_indicators_to_nan(out, interval=int(interval))
    out = attach_ready_flags(out)
    out = attach_date_time_columns(out, interval=int(interval))
    out = drop_future_datetime_rows(out, interval=int(interval), label="apply_final")

    logger.info(
        "[MTF HISTORY BOOTSTRAP] transform done interval=%s rows=%s symbols=%s "
        "score_nonzero=%s rsi_nonnull=%s macd_nonnull=%s signal_nonnull=%s "
        "slope_nonnull=%s mtf_nonnull=%s display_ready=%s technical_ready=%s "
        "hist_min=%s hist_median=%.1f hist_max=%s dt_min=%s dt_max=%s",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        int(pd.to_numeric(out["score"], errors="coerce").fillna(0).ne(0).sum()) if "score" in out.columns else 0,
        int(pd.to_numeric(out["rsi"], errors="coerce").notna().sum()) if "rsi" in out.columns else 0,
        int(pd.to_numeric(out["macd"], errors="coerce").notna().sum()) if "macd" in out.columns else 0,
        int(pd.to_numeric(out["signal"], errors="coerce").notna().sum()) if "signal" in out.columns else 0,
        int(pd.to_numeric(out["slope"], errors="coerce").notna().sum()) if "slope" in out.columns else 0,
        int(pd.to_numeric(out["mtf"], errors="coerce").notna().sum()) if "mtf" in out.columns else 0,
        int(pd.to_numeric(out["display_ready"], errors="coerce").fillna(0).ne(0).sum()) if "display_ready" in out.columns else 0,
        int(pd.to_numeric(out["technical_ready"], errors="coerce").fillna(0).ne(0).sum()) if "technical_ready" in out.columns else 0,
        int(pd.to_numeric(out["symbol_hist_len"], errors="coerce").min()) if "symbol_hist_len" in out.columns and not out.empty else 0,
        float(pd.to_numeric(out["symbol_hist_len"], errors="coerce").median()) if "symbol_hist_len" in out.columns and not out.empty else 0.0,
        int(pd.to_numeric(out["symbol_hist_len"], errors="coerce").max()) if "symbol_hist_len" in out.columns and not out.empty else 0,
        out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
        out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
    )

    return out


__all__ = [
    "apply_indicators",
    "resolve_scoring_callable",
    "apply_scoring",
    "ensure_score_aliases",
    "apply_indicators_scoring_ready",
]