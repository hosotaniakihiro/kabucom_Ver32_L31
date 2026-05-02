# ============================================================
# File   : trading/summary/postprocess/score.py
# Version: Ver1.0-PRODUCTION-POSTPROCESS-SCORE
# ------------------------------------------------------------
# ✔ score系構築
# ✔ raw slope / raw mtf と score列を分離
# ✔ final / display / score repair
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from .normalize import pick_series, safe_series

logger = logging.getLogger(__name__)


def prefer_existing_nonzero(primary: pd.Series, fallback: pd.Series) -> pd.Series:
    primary = safe_series(primary, fallback.index if isinstance(fallback, pd.Series) else None, default=0.0)
    fallback = safe_series(fallback, primary.index, default=0.0)
    try:
        keep = primary.notna() & (primary != 0)
        return primary.where(keep, fallback)
    except Exception:
        return primary


def pick_best_existing(df: pd.DataFrame, candidates, default=0.0) -> pd.Series:
    idx = df.index
    best = safe_series(None, idx, default=default)
    found = False
    for c in candidates:
        if c not in df.columns:
            continue
        cur = safe_series(df[c], idx, default=default)
        if not found:
            best = cur
            found = True
            continue
        try:
            best = best.where(best.notna() & (best != 0), cur)
        except Exception:
            pass
    return best


def pick_best_raw_slope(df: pd.DataFrame, default=0.0) -> pd.Series:
    return pick_best_existing(df, ["slope", "slope_atr_scaled", "ma75_slope"], default=default)


def pick_best_raw_mtf(df: pd.DataFrame, default=0.0) -> pd.Series:
    return pick_best_existing(df, ["mtf", "mtf_alignment"], default=default)


def pick_best_score_slope(df: pd.DataFrame, default=0.0) -> pd.Series:
    return pick_best_existing(df, ["score_slope"], default=default)


def pick_best_score_mtf(df: pd.DataFrame, default=0.0) -> pd.Series:
    return pick_best_existing(df, ["score_mtf", "mtf_score"], default=default)


def pick_best_total_score(df: pd.DataFrame, default=0.0) -> pd.Series:
    return pick_best_existing(df, ["score", "final_score", "display_score", "score_total", "combined_score"], default=default)


def restore_mtf_from_scores(raw_mtf: pd.Series, score_mtf: pd.Series, *, default: float = 0.0) -> pd.Series:
    try:
        idx = raw_mtf.index if isinstance(raw_mtf, pd.Series) else getattr(score_mtf, "index", None)
        raw_mtf = safe_series(raw_mtf, idx, default=default)

        restored = raw_mtf.copy()
        restored = restored.where(restored.abs() >= 1e-12, 0.0)
        return restored
    except Exception:
        logger.exception("[POST.SCORE] restore_mtf_from_scores failed")
        return safe_series(raw_mtf, getattr(raw_mtf, "index", None), default=default)


def clip_score_range(s: pd.Series, low: float = -14.0, high: float = 14.0) -> pd.Series:
    try:
        x = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return x.clip(lower=low, upper=high)
    except Exception:
        return safe_series(None, getattr(s, "index", None), default=0.0)


def derive_score_from_raw_signal(raw: pd.Series, *, low: float = -14.0, high: float = 14.0, deadband: float = 1e-12) -> pd.Series:
    try:
        x = pd.to_numeric(raw, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
        x = x.where(x.abs() >= deadband, 0.0)

        finite = x[np.isfinite(x)]
        if finite.empty:
            return safe_series(None, x.index, default=0.0)

        mx = float(finite.abs().max())
        if mx <= high + 1e-9:
            return x.clip(lower=low, upper=high)

        nz = finite[finite != 0]
        if nz.empty:
            return x.clip(lower=low, upper=high)

        q95 = float(np.nanpercentile(np.abs(nz), 95))
        if q95 <= 0:
            return x.clip(lower=low, upper=high)

        scaled = x / max(1.0, q95 / high)
        return scaled.clip(lower=low, upper=high)
    except Exception:
        logger.exception("[POST.SCORE] derive_score_from_raw_signal failed")
        return safe_series(None, getattr(raw, "index", None), default=0.0)


def prefer_existing_or_derived_score(existing_score: pd.Series, raw_signal: pd.Series, *, default: float = 0.0) -> pd.Series:
    try:
        idx = raw_signal.index if isinstance(raw_signal, pd.Series) else None
        existing_score = safe_series(existing_score, idx, default=default)
        derived = derive_score_from_raw_signal(safe_series(raw_signal, idx, default=0.0))
        keep = existing_score.notna() & (existing_score != 0)
        return existing_score.where(keep, derived)
    except Exception:
        logger.exception("[POST.SCORE] prefer_existing_or_derived_score failed")
        return safe_series(existing_score, getattr(existing_score, "index", None), default=default)


def build_single_tf_score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    idx = df.index

    score_total = pick_best_existing(
        df,
        ["score_total", "combined_score", "final_score", "score"],
        default=0.0,
    )

    raw_score_buy = pick_best_existing(df, ["score_buy", "buy_score"], default=0.0)
    raw_score_sell = pick_best_existing(df, ["score_sell", "sell_score"], default=0.0)

    df["score_buy"] = safe_series(raw_score_buy, idx, default=0.0).clip(lower=0)
    df["score_sell"] = safe_series(raw_score_sell, idx, default=0.0).abs()
    df["buy_score"] = safe_series(df["score_buy"], idx, default=0.0)
    df["sell_score"] = safe_series(df["score_sell"], idx, default=0.0)

    raw_slope = pick_best_raw_slope(df, default=0.0)
    df["slope_atr_scaled"] = safe_series(raw_slope, idx, default=0.0)
    df["ma75_slope"] = pick_best_existing(df, ["ma75_slope", "slope_atr_scaled"], default=0.0)

    existing_score_slope = pick_best_score_slope(df, default=0.0)
    df["score_slope"] = prefer_existing_or_derived_score(
        existing_score_slope,
        raw_slope,
        default=0.0,
    )

    if (score_total != 0).any():
        combined = score_total
    else:
        combined = safe_series(df["score_buy"], idx, default=0.0) - safe_series(df["score_sell"], idx, default=0.0)

    df["combined_score"] = safe_series(combined, idx, default=0.0)
    df["base_score"] = safe_series(df["combined_score"], idx, default=0.0)
    return df


def build_mtf_score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    idx = df.index

    score_1m = pick_series(df, ["combined_score"], default=0.0)
    score_3m = pick_series(df, ["combined_score_3m", "score_3m"], default=0.0)
    score_5m = pick_series(df, ["combined_score_5m", "score_5m"], default=0.0)

    raw_mtf = pick_best_raw_mtf(df, default=0.0)
    existing_score_mtf = pick_best_score_mtf(df, default=0.0)
    slope_score = pick_best_score_slope(df, default=0.0)

    derived_score_mtf = prefer_existing_or_derived_score(
        existing_score_mtf,
        raw_mtf,
        default=0.0,
    )

    weighted_mtf = (
        safe_series(score_1m, idx, default=0.0) * 0.60
        + safe_series(score_3m, idx, default=0.0) * 0.25
        + safe_series(score_5m, idx, default=0.0) * 0.15
    )

    fallback_mtf = clip_score_range(derived_score_mtf + slope_score * 0.20)

    mtf_score = weighted_mtf.where(weighted_mtf != 0, fallback_mtf)
    mtf_score = derived_score_mtf.where(derived_score_mtf != 0, mtf_score)

    df["mtf_score"] = clip_score_range(safe_series(mtf_score, idx, default=0.0))
    df["score_mtf"] = safe_series(df["mtf_score"], idx, default=0.0)

    df["mtf"] = restore_mtf_from_scores(raw_mtf, df["score_mtf"], default=0.0)

    return df


def build_ai_score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    idx = df.index

    lgb_score = pick_series(df, ["lgbm_score", "model_score"], default=0.0)
    confidence = pick_series(df, ["confidence", "ai_confidence"], default=0.0)

    df["ai_score"] = safe_series(lgb_score * (1.0 + confidence), idx, default=0.0)
    return df


def build_final_score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    idx = df.index

    existing_final = pick_best_existing(df, ["final_score", "display_score", "score_total", "combined_score", "score"], default=0.0)
    existing_display = pick_best_existing(df, ["display_score", "final_score", "score_total", "combined_score", "score"], default=0.0)
    existing_score = pick_best_existing(df, ["score", "final_score", "display_score", "score_total", "combined_score"], default=0.0)

    existing_mtf = pick_best_raw_mtf(df, default=0.0)
    existing_slope = pick_best_raw_slope(df, default=0.0)

    mtf_score = pick_best_score_mtf(df, default=0.0)
    slope_score = pick_best_score_slope(df, default=0.0)
    ai_score = pick_series(df, ["ai_score"], default=0.0)
    combined_score = pick_best_existing(df, ["combined_score", "score_total", "score"], default=0.0)

    calc_final = clip_score_range(
        combined_score * 0.50
        + mtf_score * 0.35
        + slope_score * 0.15
        + ai_score * 0.10
    )

    if (existing_final != 0).any():
        final_score = prefer_existing_nonzero(existing_final, calc_final)
    elif (combined_score != 0).any():
        final_score = combined_score
    else:
        final_score = calc_final

    df["final_score"] = clip_score_range(safe_series(final_score, idx, default=0.0))

    if (existing_display != 0).any():
        df["display_score"] = safe_series(existing_display.abs(), idx, default=0.0)
    else:
        df["display_score"] = safe_series(df["final_score"].abs(), idx, default=0.0)

    if (existing_score != 0).any():
        df["score"] = prefer_existing_nonzero(existing_score, df["final_score"])
    else:
        df["score"] = safe_series(df["final_score"], idx, default=0.0)

    df["mtf"] = restore_mtf_from_scores(existing_mtf, mtf_score, default=0.0)
    df["slope"] = safe_series(existing_slope, idx, default=0.0)

    return df


def build_score_reason(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    reasons = []
    for _, row in df.iterrows():
        r = []

        if float(row.get("combined_score", 0) or 0) > 0:
            r.append("BaseScore")
        if float(row.get("slope_atr_scaled", 0) or 0) > 0:
            r.append("Slope↑")
        if float(row.get("score_mtf", 0) or 0) > 0:
            r.append("MTF+")
        if float(row.get("lgbm_score", 0) or 0) > 0:
            r.append("AI")
        if float(row.get("confidence", 0) or 0) > 0.5:
            r.append("HighConf")

        reasons.append(",".join(r) if r else "")

    df["score_reason"] = reasons
    return df


def assign_cluster(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["cluster"] = df["market_regime"].astype(str) if "market_regime" in df.columns else "default"
    return df