# ============================================================
# File   : trading/summary/summary_post_processor.py
# Version: Ver16.0-PRODUCTION-POSTPROCESS-MODULARIZED
# ------------------------------------------------------------
# ✔ summary post process の薄い統合ファイル
# ✔ normalize / filter / calc / score を内部モジュールへ分割
# ✔ 既存API: post_process_summary(df) を維持
# ✔ scoreがあっても technical全滅なら再計算
# ✔ mtf は raw 系列のみ表示
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import numpy as np
import pandas as pd

from trading.summary.postprocess.normalize import (
    ensure_dataframe,
    normalize_basic,
    ensure_price_columns,
    ensure_indicator_columns,
    protect_name_columns,
    pick_series,
    safe_series,
)
from trading.summary.postprocess.filtering import (
    drop_outside_allowed_dates,
    apply_market_filter_df,
    drop_dead_rows,
    deduplicate,
)
from trading.summary.postprocess.calc import (
    needs_actual_calc,
    run_actual_indicator_and_scoring,
)
from trading.summary.postprocess.score import (
    prefer_existing_nonzero,
    pick_best_existing,
    pick_best_raw_slope,
    pick_best_raw_mtf,
    pick_best_score_slope,
    pick_best_score_mtf,
    pick_best_total_score,
    prefer_existing_or_derived_score,
    clip_score_range,
    build_single_tf_score,
    build_mtf_score,
    build_ai_score,
    build_final_score,
    build_score_reason,
    assign_cluster,
)

logger = logging.getLogger(__name__)


def _is_today_business_day() -> bool:
    try:
        from utils.business_day_utils import is_today_business_day
        return bool(is_today_business_day())
    except Exception:
        return dt.date.today().weekday() < 5


def _is_closed_day_mode(df: pd.DataFrame) -> bool:
    try:
        if _is_today_business_day():
            return False
        return True
    except Exception:
        return False


def _series_is_fixed_value_cluster(s: pd.Series, *, min_rows: int = 20, ratio_threshold: float = 0.6) -> bool:
    try:
        x = pd.to_numeric(s, errors="coerce").dropna()
        if len(x) < min_rows:
            return False

        nz = x[x != 0]
        if len(nz) < min_rows:
            return False

        vc = nz.round(6).value_counts(dropna=True)
        if vc.empty:
            return False

        top_ratio = float(vc.iloc[0] / len(nz))
        unique_n = int(vc.size)
        top_val = float(vc.index[0])

        suspicious_14 = abs(abs(top_val) - 14.0) <= 1e-6
        suspicious_ratio = top_ratio >= ratio_threshold
        suspicious_unique = unique_n <= 3 and top_ratio >= 0.5

        bad = suspicious_14 or suspicious_ratio or suspicious_unique

        logger.info(
            "[POST] preserve quality check rows=%d nz=%d top_val=%s top_ratio=%.3f unique_n=%d bad=%s",
            len(x), len(nz), top_val, top_ratio, unique_n, bad
        )
        return bad
    except Exception:
        logger.exception("[POST] fixed value cluster check failed")
        return False


def _allow_preserve_series(s: pd.Series, *, closed_day_mode: bool) -> bool:
    try:
        x = pd.to_numeric(s, errors="coerce").fillna(0.0)

        if int((x != 0).sum()) == 0:
            return False

        if not closed_day_mode:
            return True

        if _series_is_fixed_value_cluster(x):
            return False

        return True
    except Exception:
        logger.exception("[POST] allow preserve series failed")
        return False


def _log_profile(df: pd.DataFrame, stage: str) -> None:
    try:
        if df is None or df.empty:
            logger.info("[POST] %s empty", stage)
            return

        logger.info(
            "[POST] %s rows=%s cols=%s symbols=%s latest_dt=%s",
            stage,
            len(df),
            len(df.columns),
            int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
            df["datetime"].max() if "datetime" in df.columns else None,
        )

        for col in [
            "score_total",
            "buy_score", "sell_score",
            "score_buy", "score_sell",
            "combined_score",
            "ma75_slope", "slope_atr_scaled", "score_slope",
            "mtf_score", "score_mtf",
            "ai_score", "final_score", "display_score",
            "score", "slope", "mtf",
            "open", "high", "low", "close",
            "rsi", "macd", "signal",
        ]:
            if col in df.columns:
                s = pd.to_numeric(df[col], errors="coerce")
                logger.info(
                    "[POST] %s %s non_null=%s nonzero=%s nunique=%s min=%s max=%s",
                    stage, col,
                    int(s.notna().sum()),
                    int((s.fillna(0) != 0).sum()),
                    int(s.nunique(dropna=True)),
                    s.min(), s.max(),
                )
            else:
                logger.info("[POST] %s %s=MISSING", stage, col)
    except Exception:
        logger.exception("[POST] profile log failed stage=%s", stage)


def _log_zero_profile(df: pd.DataFrame, stage: str) -> None:
    try:
        if df is None or df.empty:
            logger.info("[POST] %s zero profile skipped empty", stage)
            return

        parts = []
        for col in ("score", "score_total", "slope", "mtf", "score_buy", "score_sell", "score_mtf", "score_slope"):
            if col in df.columns:
                s = pd.to_numeric(df[col], errors="coerce").fillna(0)
                parts.append(f"{col}={int((s == 0).sum())}/{len(df)}")

        logger.info("[POST] %s zero_profile %s", stage, " ".join(parts))
    except Exception:
        logger.exception("[POST] zero profile log failed stage=%s", stage)


def _log_raw_sample(df: pd.DataFrame, stage: str) -> None:
    try:
        if df is None or df.empty:
            logger.info("[POST] %s raw sample empty", stage)
            return

        cols = [
            "symbol", "symbolname", "name",
            "score_total", "buy_score", "sell_score",
            "score_buy", "score_sell", "combined_score",
            "score_slope", "slope_atr_scaled",
            "score_mtf", "mtf_score",
            "final_score", "display_score", "score",
            "slope", "mtf",
            "open", "high", "low", "close",
            "rsi", "macd", "signal",
        ]
        existing = [c for c in cols if c in df.columns]

        logger.info("[POST] %s raw cols -> %s", stage, existing)
        logger.info("[POST] %s raw sample\n%s", stage, df[existing].head(20).to_string())
    except Exception:
        logger.exception("[POST] raw sample log failed stage=%s", stage)


def post_process_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()

    try:
        df = ensure_dataframe(df)
        df = normalize_basic(df)

        if df.empty:
            return pd.DataFrame()

        df = ensure_price_columns(df)
        df = ensure_indicator_columns(df)
        df = apply_market_filter_df(df)
        df = drop_outside_allowed_dates(df, "after_market_filter")
        if df.empty:
            logger.warning("[POST] dataframe empty after market filter/date guard")
            return pd.DataFrame()

        _log_profile(df, "before")
        _log_zero_profile(df, "before")
        _log_raw_sample(df, "before")

        closed_day_mode = _is_closed_day_mode(df)

        preserved_score = pick_best_total_score(df, default=0.0)
        preserved_final = pick_best_existing(df, ["final_score", "display_score", "score_total", "combined_score", "score"], default=0.0)
        preserved_display = pick_best_existing(df, ["display_score", "final_score", "score_total", "combined_score", "score"], default=0.0)

        preserved_mtf = pick_best_raw_mtf(df, default=0.0)
        preserved_mtf_score = pick_best_score_mtf(df, default=0.0)
        preserved_slope = pick_best_raw_slope(df, default=0.0)
        preserved_slope_score = pick_best_score_slope(df, default=0.0)

        preserved_buy = pick_best_existing(df, ["score_buy", "buy_score"], default=0.0).clip(lower=0)
        preserved_sell = pick_best_existing(df, ["score_sell", "sell_score"], default=0.0).abs()

        allow_preserve_score = _allow_preserve_series(preserved_score, closed_day_mode=closed_day_mode)
        allow_preserve_final = _allow_preserve_series(preserved_final, closed_day_mode=closed_day_mode)
        allow_preserve_display = _allow_preserve_series(preserved_display, closed_day_mode=closed_day_mode)
        allow_preserve_mtf = _allow_preserve_series(preserved_mtf, closed_day_mode=closed_day_mode)
        allow_preserve_mtf_score = _allow_preserve_series(preserved_mtf_score, closed_day_mode=closed_day_mode)
        allow_preserve_slope = _allow_preserve_series(preserved_slope, closed_day_mode=closed_day_mode)
        allow_preserve_slope_score = _allow_preserve_series(preserved_slope_score, closed_day_mode=closed_day_mode)
        allow_preserve_buy = _allow_preserve_series(preserved_buy, closed_day_mode=closed_day_mode)
        allow_preserve_sell = _allow_preserve_series(preserved_sell, closed_day_mode=closed_day_mode)

        logger.info(
            "[POST] preserve flags closed_day=%s score=%s final=%s display=%s mtf=%s mtf_score=%s slope=%s slope_score=%s buy=%s sell=%s",
            closed_day_mode,
            allow_preserve_score,
            allow_preserve_final,
            allow_preserve_display,
            allow_preserve_mtf,
            allow_preserve_mtf_score,
            allow_preserve_slope,
            allow_preserve_slope_score,
            allow_preserve_buy,
            allow_preserve_sell,
        )

        if needs_actual_calc(df):
            calc_df = run_actual_indicator_and_scoring(df)
            if calc_df is not None and not calc_df.empty:
                df = calc_df
                df = ensure_dataframe(df)
                df = normalize_basic(df)
                df = ensure_price_columns(df)
                df = ensure_indicator_columns(df)
                df = apply_market_filter_df(df)
                df = drop_outside_allowed_dates(df, "after_actual_calc")
                logger.info("[POST] actual indicator/scoring applied rows=%s", len(df))
            else:
                logger.warning("[POST] actual indicator/scoring returned empty -> keep original flow")

        df = build_single_tf_score(df)
        df = build_mtf_score(df)
        df = build_ai_score(df)
        df = build_final_score(df)
        df = build_score_reason(df)
        df = assign_cluster(df)

        if allow_preserve_buy and (preserved_buy != 0).any():
            df["score_buy"] = prefer_existing_nonzero(df.get("score_buy", preserved_buy), preserved_buy)
            df["buy_score"] = safe_series(df["score_buy"], df.index, default=0.0)

        if allow_preserve_sell and (preserved_sell != 0).any():
            df["score_sell"] = prefer_existing_nonzero(df.get("score_sell", preserved_sell), preserved_sell)
            df["sell_score"] = safe_series(df["score_sell"], df.index, default=0.0)

        if allow_preserve_slope_score and (preserved_slope_score != 0).any():
            df["score_slope"] = prefer_existing_nonzero(df.get("score_slope", preserved_slope_score), preserved_slope_score)

        if allow_preserve_slope and (preserved_slope != 0).any():
            df["slope_atr_scaled"] = prefer_existing_nonzero(df.get("slope_atr_scaled", preserved_slope), preserved_slope)
            df["slope"] = prefer_existing_nonzero(df.get("slope", preserved_slope), preserved_slope)

        if allow_preserve_mtf_score and (preserved_mtf_score != 0).any():
            df["mtf_score"] = prefer_existing_nonzero(df.get("mtf_score", preserved_mtf_score), preserved_mtf_score)
            df["score_mtf"] = prefer_existing_nonzero(df.get("score_mtf", preserved_mtf_score), preserved_mtf_score)

        if allow_preserve_mtf and (preserved_mtf != 0).any():
            df["mtf"] = prefer_existing_nonzero(df.get("mtf", preserved_mtf), preserved_mtf)
        else:
            df["mtf"] = safe_series(
                pick_best_raw_mtf(df, default=0.0),
                df.index,
                default=0.0,
            )

        if allow_preserve_final and (preserved_final != 0).any():
            df["final_score"] = prefer_existing_nonzero(df.get("final_score", preserved_final), preserved_final)

        if allow_preserve_display and (preserved_display != 0).any():
            df["display_score"] = prefer_existing_nonzero(df.get("display_score", preserved_display), preserved_display.abs())

        if allow_preserve_score and (preserved_score != 0).any():
            df["score"] = prefer_existing_nonzero(df.get("score", preserved_score), preserved_score)

        df = drop_outside_allowed_dates(df, "before_dead_drop")
        df = drop_dead_rows(df)
        df = deduplicate(df)

        idx = df.index

        if "score_buy" not in df.columns:
            df["score_buy"] = safe_series(None, idx, default=0.0)
        if "score_sell" not in df.columns:
            df["score_sell"] = safe_series(None, idx, default=0.0)
        if "score_slope" not in df.columns:
            df["score_slope"] = safe_series(None, idx, default=0.0)
        if "score_mtf" not in df.columns:
            df["score_mtf"] = safe_series(None, idx, default=0.0)
        if "mtf_score" not in df.columns:
            df["mtf_score"] = safe_series(None, idx, default=0.0)
        if "final_score" not in df.columns:
            df["final_score"] = safe_series(None, idx, default=0.0)
        if "display_score" not in df.columns:
            df["display_score"] = safe_series(None, idx, default=0.0)
        if "score" not in df.columns:
            df["score"] = safe_series(None, idx, default=0.0)

        df["score_buy"] = pick_best_existing(df, ["score_buy", "buy_score"], default=0.0).clip(lower=0)
        df["score_sell"] = pick_best_existing(df, ["score_sell", "sell_score"], default=0.0).abs()
        df["buy_score"] = safe_series(df["score_buy"], idx, default=0.0)
        df["sell_score"] = safe_series(df["score_sell"], idx, default=0.0)

        raw_slope_final = pick_best_raw_slope(df, default=0.0)
        raw_mtf_final = pick_best_raw_mtf(df, default=0.0)

        df["score_slope"] = prefer_existing_or_derived_score(
            pick_best_score_slope(df, default=0.0),
            raw_slope_final,
            default=0.0,
        )
        df["mtf_score"] = prefer_existing_or_derived_score(
            pick_best_score_mtf(df, default=0.0),
            raw_mtf_final,
            default=0.0,
        )
        df["score_mtf"] = safe_series(df["mtf_score"], idx, default=0.0)

        df["slope_atr_scaled"] = safe_series(raw_slope_final, idx, default=0.0)
        df["slope"] = safe_series(raw_slope_final, idx, default=0.0)
        df["mtf"] = safe_series(raw_mtf_final, idx, default=0.0)

        calc_final_repair = clip_score_range(
            pick_best_existing(df, ["combined_score", "score_total"], default=0.0) * 0.50
            + pick_best_score_mtf(df, default=0.0) * 0.35
            + pick_best_score_slope(df, default=0.0) * 0.15
            + pick_series(df, ["ai_score"], default=0.0) * 0.10
        )

        if "final_score" in df.columns:
            df["final_score"] = prefer_existing_nonzero(df["final_score"], calc_final_repair)
        else:
            df["final_score"] = calc_final_repair

        if "score" in df.columns:
            df["score"] = prefer_existing_nonzero(df["score"], df["final_score"])
        else:
            df["score"] = safe_series(df["final_score"], idx, default=0.0)

        if "display_score" in df.columns:
            df["display_score"] = prefer_existing_nonzero(
                df["display_score"],
                safe_series(df["final_score"].abs(), idx, default=0.0),
            )
        else:
            df["display_score"] = safe_series(df["final_score"].abs(), idx, default=0.0)

        if closed_day_mode and _series_is_fixed_value_cluster(df["score"]):
            logger.warning("[POST] closed-day suspicious score cluster detected -> fallback final_score")
            df["score"] = pick_best_existing(df, ["final_score", "combined_score", "score_total"], default=0.0)

        if closed_day_mode and _series_is_fixed_value_cluster(df["display_score"]):
            logger.warning("[POST] closed-day suspicious display_score cluster detected -> fallback final_score.abs()")
            df["display_score"] = pick_best_existing(df, ["final_score", "score", "combined_score"], default=0.0).abs()

        df["final_score"] = pick_best_existing(df, ["final_score", "display_score", "score_total", "combined_score", "score"], default=0.0)
        df["display_score"] = pick_best_existing(df, ["display_score", "final_score", "score_total", "combined_score", "score"], default=0.0).abs()
        df["score"] = pick_best_existing(df, ["score", "final_score", "combined_score", "score_total"], default=0.0)
        df["score_total"] = pick_best_existing(df, ["score_total", "combined_score", "final_score", "score"], default=0.0)

        df["mtf"] = safe_series(
            pick_best_existing(df, ["mtf", "mtf_alignment"], default=0.0),
            df.index,
            default=0.0,
        )

        df = ensure_price_columns(df)
        df = ensure_indicator_columns(df)
        df = protect_name_columns(df)
        df = drop_outside_allowed_dates(df, "final")

        _log_profile(df, "after")
        _log_zero_profile(df, "after")
        _log_raw_sample(df, "after")

        return df.reset_index(drop=True)

    except Exception:
        logger.exception("[POST] post_process_summary failed")
        return pd.DataFrame()