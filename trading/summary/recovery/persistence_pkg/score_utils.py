# ============================================================
# File   : trading/summary/recovery/persistence_pkg/score_utils.py
# Ver    : PRODUCTION-STABLE-REV9.0-SCORE-UTILS
# ------------------------------------------------------------
# 【概要】
#   score / buy / sell fallback helpers
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .column_utils import pick_first_existing, pick_numeric_series_nan

logger = logging.getLogger(__name__)


def build_score_from_buy_sell(df: pd.DataFrame) -> pd.Series:
    idx = df.index

    buy_s = pick_numeric_series_nan(df, ["score_buy", "buy_score", "buy"])
    sell_s = pick_numeric_series_nan(df, ["score_sell", "sell_score", "sell"])

    out = pd.Series(float("nan"), index=idx, dtype="float64")

    try:
        out = out.combine_first(buy_s)
    except Exception:
        out = buy_s.copy()

    try:
        out = out.combine_first(sell_s)
    except Exception:
        out = out.where(out.notna(), sell_s)

    try:
        both = buy_s.notna() & sell_s.notna()
        if both.any():
            choose_sell = sell_s.abs() > buy_s.abs()
            out.loc[both & choose_sell] = sell_s.loc[both & choose_sell]
            out.loc[both & ~choose_sell] = buy_s.loc[both & ~choose_sell]
    except Exception:
        pass

    return out


def ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    score_from_buy_sell = build_score_from_buy_sell(out)

    if "score" not in out.columns:
        alt = pick_first_existing(out, ["score_total", "display_score", "final_score", "combined_score"])
        if alt:
            out["score"] = pd.to_numeric(out[alt], errors="coerce")
        else:
            out["score"] = score_from_buy_sell
    else:
        score_s = pd.to_numeric(out["score"], errors="coerce")
        out["score"] = score_s.combine_first(score_from_buy_sell)

    if "score_total" not in out.columns:
        out["score_total"] = pd.to_numeric(out["score"], errors="coerce")
    else:
        out["score_total"] = pd.to_numeric(out["score_total"], errors="coerce").combine_first(
            pd.to_numeric(out["score"], errors="coerce")
        )

    if "final_score" not in out.columns:
        out["final_score"] = pd.to_numeric(out["score"], errors="coerce")
    else:
        out["final_score"] = pd.to_numeric(out["final_score"], errors="coerce").combine_first(
            pd.to_numeric(out["score"], errors="coerce")
        )

    if "display_score" not in out.columns:
        out["display_score"] = pd.to_numeric(out["score"], errors="coerce")
    else:
        out["display_score"] = pd.to_numeric(out["display_score"], errors="coerce").combine_first(
            pd.to_numeric(out["score"], errors="coerce")
        )

    if "score_buy" not in out.columns:
        alt = pick_first_existing(out, ["buy_score", "buy"])
        if alt:
            out["score_buy"] = pd.to_numeric(out[alt], errors="coerce")
    else:
        buy_alt = pick_numeric_series_nan(out, ["buy_score", "buy"])
        out["score_buy"] = pd.to_numeric(out["score_buy"], errors="coerce").combine_first(buy_alt)

    if "buy_score" not in out.columns and "score_buy" in out.columns:
        out["buy_score"] = pd.to_numeric(out["score_buy"], errors="coerce")

    if "score_sell" not in out.columns:
        alt = pick_first_existing(out, ["sell_score", "sell"])
        if alt:
            out["score_sell"] = pd.to_numeric(out[alt], errors="coerce")
    else:
        sell_alt = pick_numeric_series_nan(out, ["sell_score", "sell"])
        out["score_sell"] = pd.to_numeric(out["score_sell"], errors="coerce").combine_first(sell_alt)

    if "sell_score" not in out.columns and "score_sell" in out.columns:
        out["sell_score"] = pd.to_numeric(out["score_sell"], errors="coerce")

    return out


def is_completed_summary_df(df: pd.DataFrame) -> bool:
    try:
        if df is None or df.empty:
            return False
        if "symbol" not in df.columns:
            return False

        symbol_s = df["symbol"].fillna("").astype(str).str.strip()
        if symbol_s.eq("").all():
            return False

        score_s = pick_numeric_series_nan(df, ["score", "score_total", "display_score", "final_score"])
        buy_s = pick_numeric_series_nan(df, ["score_buy", "buy_score", "buy"])
        sell_s = pick_numeric_series_nan(df, ["score_sell", "sell_score", "sell"])

        score_nonnull = int(score_s.notna().sum())
        buy_nonnull = int(buy_s.notna().sum())
        sell_nonnull = int(sell_s.notna().sum())

        if score_nonnull == 0 and buy_nonnull == 0 and sell_nonnull == 0:
            return False

        return True
    except Exception:
        return False


def repair_mtf_consistency(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    try:
        mtf_s = pick_numeric_series_nan(out, ["mtf", "mtf_alignment"])
        score_mtf_s = pick_numeric_series_nan(out, ["score_mtf", "mtf_score"])
        total_s = pick_numeric_series_nan(out, ["score", "score_total", "display_score"])
        final_s = pick_numeric_series_nan(out, ["final_score", "display_score"])

        bad_mask = mtf_s.fillna(0).eq(0)

        if "score_mtf" in out.columns:
            m = bad_mask & score_mtf_s.fillna(0).gt(0)
            if m.any():
                out.loc[m, "score_mtf"] = 0.0

        if "mtf_score" in out.columns:
            m = bad_mask & score_mtf_s.fillna(0).gt(0)
            if m.any():
                out.loc[m, "mtf_score"] = 0.0

        if "final_score" in out.columns:
            m = bad_mask & final_s.fillna(0).gt(0) & total_s.notna()
            if m.any():
                out.loc[m, "final_score"] = total_s[m]
    except Exception:
        logger.exception("[summary.recovery.persistence] repair_mtf_consistency failed")
    return out


__all__ = [
    "build_score_from_buy_sell",
    "ensure_score_columns",
    "is_completed_summary_df",
    "repair_mtf_consistency",
]