# ============================================================
# File   : trading/scoring/core/scoring_pipeline.py
# Version: Ver1.3-PRODUCTION-SCORING-PIPELINE-SIDE-SCORE-REPAIR
# ------------------------------------------------------------
# ✔ scoring pipeline public entrypoint
# ✔ backward compatibility for run_scoring_pipeline
# ✔ scoring_main public alias
# ✔ interval / analysis_only / force kwargs safe ignore
# ✔ detail score columns support
# ✔ score / buy / sell / total / final support
# ✔ score_slope / score_mtf support
# ✔ production safe
# ✔ FIX: detail列の NaN preserve
# ✔ FIX: total合成時のみ一時 fillna(0.0)
# ✔ FIX: 既存 total/score/final を不必要に上書きしない
# ✔ FIX: display_score は total の符号を維持
# ✔ FIX: passthrough テクニカル列を破壊しない
# ✔ Ver1.3: score_buy / score_sell が既存0で埋まっているだけの場合、
#            signed total score から安全に再補完する。
# ============================================================

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# dataframe helpers
# ============================================================

def _ensure_dataframe(df) -> pd.DataFrame:
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
            out = pd.DataFrame(df).copy()
        except Exception:
            logger.exception("[SCORING] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if str(x) not in ("", "None")]).strip("_")
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        logger.debug("[SCORING] multiindex flatten failed", exc_info=True)

    try:
        if out.columns.duplicated().any():
            out = out.loc[:, ~out.columns.duplicated()].copy()
    except Exception:
        logger.debug("[SCORING] duplicate column cleanup failed", exc_info=True)

    return out.reset_index(drop=True)


def _safe_num(v, default=np.nan) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, str) and not v.strip():
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _safe_series(df: pd.DataFrame, col: str, default=np.nan) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if isinstance(df, pd.DataFrame) else None, dtype="float64")
    try:
        s = pd.to_numeric(df[col], errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        return s
    except Exception:
        logger.debug("[SCORING] safe_series failed col=%s", col, exc_info=True)
        return pd.Series(default, index=df.index, dtype="float64")


def _pick_series(df: pd.DataFrame, cols: list[str], default=np.nan) -> pd.Series:
    for c in cols:
        if c in df.columns:
            return _safe_series(df, c, default=default)
    return pd.Series(default, index=df.index, dtype="float64")


def _fillna_num(series: pd.Series, default: float = 0.0) -> pd.Series:
    try:
        s = pd.to_numeric(series, errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        return s.fillna(default)
    except Exception:
        return pd.Series(default, index=series.index if hasattr(series, "index") else None, dtype="float64")


def _nonnull_count(s: pd.Series) -> int:
    try:
        return int(pd.to_numeric(s, errors="coerce").notna().sum())
    except Exception:
        return 0


def _nonzero_count(s: pd.Series) -> int:
    try:
        return int(pd.to_numeric(s, errors="coerce").fillna(0.0).ne(0).sum())
    except Exception:
        return 0


def _has_positive(s: pd.Series) -> bool:
    try:
        return bool(pd.to_numeric(s, errors="coerce").fillna(0.0).gt(0.0).any())
    except Exception:
        return False


def _has_negative(s: pd.Series) -> bool:
    try:
        return bool(pd.to_numeric(s, errors="coerce").fillna(0.0).lt(0.0).any())
    except Exception:
        return False


# ============================================================
# detail score normalization
# ============================================================

def _normalize_detail_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["base"] = _pick_series(out, ["base", "score_base"], default=np.nan)
    out["trend"] = _pick_series(out, ["trend", "score_trend"], default=np.nan)
    out["mom"] = _pick_series(out, ["mom", "score_momentum", "momentum"], default=np.nan)
    out["vel"] = _pick_series(out, ["vel", "score_velocity", "velocity"], default=np.nan)
    out["pen"] = _pick_series(out, ["pen", "direction_penalty", "penalty"], default=np.nan)

    out["score_slope"] = _pick_series(out, ["score_slope", "slope_score", "slope"], default=np.nan)
    out["score_mtf"] = _pick_series(out, ["score_mtf", "mtf_score", "mtf"], default=np.nan)

    return out


# ============================================================
# score calculation core
# ============================================================

def _build_total_score(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = _normalize_detail_columns(out)

    base = _safe_series(out, "base", default=np.nan)
    trend = _safe_series(out, "trend", default=np.nan)
    mom = _safe_series(out, "mom", default=np.nan)
    vel = _safe_series(out, "vel", default=np.nan)
    pen = _safe_series(out, "pen", default=np.nan)

    score_slope_raw = _safe_series(out, "score_slope", default=np.nan)
    score_mtf_raw = _safe_series(out, "score_mtf", default=np.nan)

    total_raw = _fillna_num(base, 0.0) + _fillna_num(trend, 0.0) + _fillna_num(mom, 0.0) + _fillna_num(vel, 0.0) - _fillna_num(pen, 0.0)
    total_score_synth = total_raw + _fillna_num(score_slope_raw, 0.0) + _fillna_num(score_mtf_raw, 0.0)

    existing_total = _safe_series(out, "score_total", default=np.nan)
    existing_score = _safe_series(out, "score", default=np.nan)
    existing_final = _safe_series(out, "final_score", default=np.nan)
    existing_combined = _safe_series(out, "combined_score", default=np.nan)
    existing_display = _safe_series(out, "display_score", default=np.nan)
    existing_buy = _safe_series(out, "score_buy", default=np.nan)
    existing_sell = _safe_series(out, "score_sell", default=np.nan)

    resolved_total = existing_total.copy()
    resolved_total = resolved_total.where(resolved_total.notna(), existing_score)
    resolved_total = resolved_total.where(resolved_total.notna(), existing_final)
    resolved_total = resolved_total.where(resolved_total.notna(), existing_combined)
    resolved_total = resolved_total.where(resolved_total.notna(), total_score_synth)

    out["score_total"] = resolved_total
    out["combined_score"] = existing_combined.where(existing_combined.notna(), resolved_total)
    out["final_score"] = existing_final.where(existing_final.notna(), resolved_total)
    out["display_score"] = existing_display.where(existing_display.notna(), resolved_total)
    out["score"] = existing_score.where(existing_score.notna(), resolved_total)

    buy_score_synth = resolved_total.clip(lower=0.0)
    sell_score_synth = (-resolved_total).clip(lower=0.0)

    buy_present = existing_buy.notna()
    sell_present = existing_sell.notna()
    buy_nonzero = _nonzero_count(existing_buy)
    sell_nonzero = _nonzero_count(existing_sell)
    total_has_positive = _has_positive(resolved_total)
    total_has_negative = _has_negative(resolved_total)

    # 既存の score_buy/score_sell が 0.0 で埋まっているだけの場合は「実質欠損」とみなす。
    # これにより score が正なのに score_buy が全件0、または score が負なのに score_sell が全件0になる状態を修復する。
    if buy_nonzero == 0 and total_has_positive:
        out["score_buy"] = buy_score_synth
        logger.warning("[SCORING SIDE REPAIR] score_buy all-zero repaired from signed total rows=%s positive=%s", len(out), int(buy_score_synth.gt(0).sum()))
    else:
        out["score_buy"] = existing_buy.where(buy_present, buy_score_synth)

    if sell_nonzero == 0 and total_has_negative:
        out["score_sell"] = sell_score_synth
        logger.warning("[SCORING SIDE REPAIR] score_sell all-zero repaired from signed total rows=%s negative=%s", len(out), int(sell_score_synth.gt(0).sum()))
    else:
        out["score_sell"] = existing_sell.where(sell_present, sell_score_synth)

    # 片側だけが既存0で、もう片側だけ残っている場合も、符号側スコアは同期する。
    out["buy_score"] = out["score_buy"]
    out["sell_score"] = out["score_sell"]

    out["score_slope"] = score_slope_raw
    out["score_mtf"] = score_mtf_raw

    return out


# ============================================================
# optional passthrough columns
# ============================================================

def _normalize_passthrough_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "slope" not in out.columns and "score_slope" in out.columns:
        out["slope"] = pd.to_numeric(out["score_slope"], errors="coerce")

    if "mtf" not in out.columns and "score_mtf" in out.columns:
        out["mtf"] = pd.to_numeric(out["score_mtf"], errors="coerce")

    for c in ("slope", "mtf", "score_slope", "score_mtf", "rsi", "macd", "signal", "hist"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    return out


def _finalize_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for c in ("score_total", "combined_score", "final_score", "display_score", "score", "score_buy", "buy_score", "score_sell", "sell_score"):
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    for c in ("slope", "mtf", "score_slope", "score_mtf", "rsi", "macd", "signal", "hist"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    return out


# ============================================================
# main implementation
# ============================================================

def scoring_pipeline(df: pd.DataFrame, *args, **kwargs) -> pd.DataFrame:
    out = _ensure_dataframe(df)

    if out.empty:
        logger.info("[SCORING] scoring_pipeline input empty")
        return out

    out = _normalize_passthrough_columns(out)
    out = _build_total_score(out)
    out = _finalize_score_columns(out)

    try:
        from trading.scoring.core.score_breakdown import attach_score_breakdown
        out = attach_score_breakdown(out, debug=True)
    except Exception:
        logger.exception("[SCORING] attach_score_breakdown failed")

    logger.info(
        "[SCORING] scoring_pipeline done rows=%s score_nonnull=%s score_nonzero=%s buy_nonnull=%s buy_nonzero=%s sell_nonnull=%s sell_nonzero=%s slope_nonnull=%s slope_nonzero=%s mtf_nonnull=%s mtf_nonzero=%s base_nonnull=%s base_nonzero=%s trend_nonnull=%s trend_nonzero=%s mom_nonnull=%s mom_nonzero=%s vel_nonnull=%s vel_nonzero=%s pen_nonnull=%s pen_nonzero=%s",
        len(out),
        _nonnull_count(out["score"]) if "score" in out.columns else 0,
        _nonzero_count(out["score"]) if "score" in out.columns else 0,
        _nonnull_count(out["score_buy"]) if "score_buy" in out.columns else 0,
        _nonzero_count(out["score_buy"]) if "score_buy" in out.columns else 0,
        _nonnull_count(out["score_sell"]) if "score_sell" in out.columns else 0,
        _nonzero_count(out["score_sell"]) if "score_sell" in out.columns else 0,
        _nonnull_count(out["score_slope"]) if "score_slope" in out.columns else 0,
        _nonzero_count(out["score_slope"]) if "score_slope" in out.columns else 0,
        _nonnull_count(out["score_mtf"]) if "score_mtf" in out.columns else 0,
        _nonzero_count(out["score_mtf"]) if "score_mtf" in out.columns else 0,
        _nonnull_count(out["score_base"]) if "score_base" in out.columns else 0,
        _nonzero_count(out["score_base"]) if "score_base" in out.columns else 0,
        _nonnull_count(out["score_trend"]) if "score_trend" in out.columns else 0,
        _nonzero_count(out["score_trend"]) if "score_trend" in out.columns else 0,
        _nonnull_count(out["score_momentum"]) if "score_momentum" in out.columns else 0,
        _nonzero_count(out["score_momentum"]) if "score_momentum" in out.columns else 0,
        _nonnull_count(out["score_velocity"]) if "score_velocity" in out.columns else 0,
        _nonzero_count(out["score_velocity"]) if "score_velocity" in out.columns else 0,
        _nonnull_count(out["score_penalty"]) if "score_penalty" in out.columns else 0,
        _nonzero_count(out["score_penalty"]) if "score_penalty" in out.columns else 0,
    )

    return out


# ============================================================
# public main entrypoint
# ============================================================

def scoring_main(df, *args, **kwargs):
    kwargs.pop("interval", None)
    kwargs.pop("analysis_only", None)
    kwargs.pop("force", None)
    kwargs.pop("evaluate_signals", None)
    kwargs.pop("latest_only", None)
    kwargs.pop("recent_bars_per_symbol", None)

    try:
        return scoring_pipeline(df, *args, **kwargs)
    except TypeError:
        logger.warning("[SCORING] scoring_main TypeError -> retry without extra args")
        return scoring_pipeline(df)


def run_scoring_pipeline(df, *args, **kwargs):
    kwargs.pop("interval", None)
    kwargs.pop("analysis_only", None)
    kwargs.pop("force", None)
    kwargs.pop("evaluate_signals", None)
    kwargs.pop("latest_only", None)
    kwargs.pop("recent_bars_per_symbol", None)

    try:
        return scoring_pipeline(df, *args, **kwargs)
    except TypeError:
        logger.warning("[SCORING] run_scoring_pipeline TypeError -> retry without extra args")
        return scoring_pipeline(df)


def run_pipeline(df, *args, **kwargs):
    return run_scoring_pipeline(df, *args, **kwargs)


__all__ = ["scoring_pipeline", "scoring_main", "run_scoring_pipeline", "run_pipeline"]
