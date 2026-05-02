# ============================================================
# File   : scheduler_jobs/summary/display_normalizer.py
# Function:
#   - 表示用列作成
#   - MTF整合補正
#   - 1銘柄1行化
# ------------------------------------------------------------
# Version: Ver1.0-PRODUCTION-DISPLAY-SPLIT-NORMALIZER
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .display_base import (
    safe_df,
    coalesce_duplicate_columns,
    pick_series,
    pick_series_nan,
    pick_text_series,
    normalize_symbol_value,
    resolve_symbolname_series,
)
from .display_reasons import build_reason_series
from .display_ranking import attach_ranking_display_columns
from .display_ai import is_ai_buy_passed, is_ai_sell_passed, is_ai_exit_passed

logger = logging.getLogger(__name__)


def attach_score_breakdown_if_missing(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    required = {
        "score_base",
        "score_trend",
        "score_momentum",
        "score_velocity",
        "score_penalty",
    }

    if required.intersection(set(out.columns)):
        return out

    try:
        from trading.scoring.core.score_breakdown import attach_score_breakdown
        out2 = attach_score_breakdown(out, debug=False)
        if isinstance(out2, pd.DataFrame) and not out2.empty:
            return out2
    except Exception:
        logger.debug("[SUMMARY DISPLAY] attach_score_breakdown fallback failed", exc_info=True)

    return out


def ensure_display_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    out = coalesce_duplicate_columns(out)
    out = attach_score_breakdown_if_missing(out)
    out = coalesce_duplicate_columns(out)

    if "symbol" not in out.columns:
        return pd.DataFrame()

    out["symbol"] = out["symbol"].map(normalize_symbol_value)
    out = out[out["symbol"] != ""].copy()
    if out.empty:
        return out

    try:
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
    except Exception:
        logger.debug("[SUMMARY DISPLAY] datetime normalize failed", exc_info=True)

    out["symbolname_view"] = resolve_symbolname_series(out)

    out["disp_buy_score"] = pick_series(out, ["score_buy", "buy_score", "buy"], default=0.0)
    out["disp_sell_score"] = pick_series(out, ["score_sell", "sell_score", "sell"], default=0.0).abs()

    out["disp_score"] = pick_series(out, ["score", "display_score", "final_score"], default=0.0)
    out["disp_total_score"] = pick_series(
        out,
        ["score_total", "total_score", "combined_score", "display_score", "score", "final_score"],
        default=0.0,
    )
    if float(out["disp_total_score"].abs().sum()) == 0.0:
        out["disp_total_score"] = out["disp_buy_score"] - out["disp_sell_score"]

    out["disp_final_score"] = pick_series(
        out,
        ["final_score", "display_score", "score_total", "score"],
        default=0.0,
    )
    if float(out["disp_final_score"].abs().sum()) == 0.0:
        out["disp_final_score"] = out["disp_total_score"]

    out["disp_slope"] = pick_series(
        out,
        ["slope", "score_slope", "slope_atr_scaled", "ma75_slope"],
        default=0.0,
    )
    out["disp_score_slope"] = pick_series(
        out,
        ["score_slope", "slope_atr_scaled", "slope"],
        default=0.0,
    )

    out["disp_mtf"] = pick_series(
        out,
        ["mtf", "score_mtf", "mtf_score", "mtf_alignment"],
        default=0.0,
    )
    out["disp_score_mtf"] = pick_series(
        out,
        ["score_mtf", "mtf_score", "mtf"],
        default=0.0,
    )

    out["disp_rsi"] = pick_series_nan(out, ["rsi", "RSI"])
    out["disp_macd"] = pick_series_nan(out, ["macd", "MACD"])
    out["disp_signal"] = pick_series_nan(out, ["signal", "macd_signal", "SIGNAL"])

    out["disp_base"] = pick_series_nan(out, ["score_base", "breakdown_base", "base_score", "base", "_score_base"])
    out["disp_trend"] = pick_series_nan(out, ["score_trend", "breakdown_trend", "trend_score", "trend", "_score_trend"])
    out["disp_mom"] = pick_series_nan(
        out,
        ["score_momentum", "breakdown_mom", "score_mom", "momentum_score", "mom", "momentum", "_score_momentum"],
    )
    out["disp_vel"] = pick_series_nan(
        out,
        ["score_velocity", "breakdown_vel", "score_vel", "velocity_score", "vel", "velocity", "_score_velocity"],
    )
    out["disp_pen"] = pick_series_nan(
        out,
        ["score_penalty", "breakdown_pen", "score_pen", "penalty_score", "pen", "penalty", "direction_penalty", "direction_penalty_score", "_score_penalty"],
    )
    out["disp_close"] = pick_series_nan(out, ["close", "close_price", "current_price", "price"])

    out = attach_ranking_display_columns(out)

    out["buy_reason_ja_view"] = build_reason_series(out, side="BUY")
    out["sell_reason_ja_view"] = build_reason_series(out, side="SELL")
    out["exit_reason_ja_view"] = build_reason_series(out, side="EXIT")

    out["ai_decision_view"] = pick_text_series(
        out,
        ["ai_decision", "decision", "ai_judgement", "ai_result"],
        default="",
    ).astype(str).str.strip()

    out["ai_exit_decision_view"] = pick_text_series(
        out,
        ["ai_exit_decision", "exit_decision", "decision_exit"],
        default="",
    ).astype(str).str.strip()

    out["ai_reason_view"] = pick_text_series(
        out,
        ["ai_reason", "reason_ai", "ai_comment", "ai_message"],
        default="",
    ).astype(str).str.strip()

    out["ai_exit_reason_view"] = pick_text_series(
        out,
        ["ai_exit_reason", "reason_ai_exit", "exit_ai_reason"],
        default="",
    ).astype(str).str.strip()

    out["ai_confidence_view"] = pick_series_nan(
        out,
        ["ai_confidence", "confidence_ai", "ai_score_confidence", "ai_prob"],
    )

    out["ai_exit_confidence_view"] = pick_series_nan(
        out,
        ["ai_exit_confidence", "exit_ai_confidence", "ai_confidence_exit"],
    )

    try:
        out["ai_buy_passed_view"] = out.apply(is_ai_buy_passed, axis=1).astype(bool)
        out["ai_sell_passed_view"] = out.apply(is_ai_sell_passed, axis=1).astype(bool)
        out["ai_exit_passed_view"] = out.apply(is_ai_exit_passed, axis=1).astype(bool)
    except Exception:
        logger.debug("[SUMMARY DISPLAY] AI passed resolver failed", exc_info=True)
        out["ai_buy_passed_view"] = False
        out["ai_sell_passed_view"] = False
        out["ai_exit_passed_view"] = False

    return out.reset_index(drop=True)


def repair_mtf_consistency(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        mtf = pick_series_nan(out, ["mtf", "mtf_alignment"])
        score_mtf = pick_series_nan(out, ["score_mtf", "mtf_score"])
        final_score = pick_series_nan(out, ["final_score", "display_score"])
        total_score = pick_series_nan(out, ["score_total", "combined_score", "score"])

        bad_mask = mtf.fillna(0).eq(0)

        if "score_mtf" in out.columns:
            out.loc[bad_mask & score_mtf.fillna(0).gt(0), "score_mtf"] = 0.0
        if "mtf_score" in out.columns:
            out.loc[bad_mask & score_mtf.fillna(0).gt(0), "mtf_score"] = 0.0

        if "final_score" in out.columns:
            repl_mask = bad_mask & final_score.fillna(0).gt(0) & total_score.notna()
            if repl_mask.any():
                out.loc[repl_mask, "final_score"] = total_score[repl_mask]
    except Exception:
        logger.debug("[SUMMARY DISPLAY] mtf consistency repair failed", exc_info=True)

    return out


def dedupe_one_row_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = ensure_display_columns(df)
        if out.empty:
            return out

        dt_col = None
        for c in ("datetime", "end_time", "start_time", "time"):
            if c in out.columns:
                dt_col = c
                break

        complete_score = pd.Series(0, index=out.index, dtype="int64")
        for c, w in [
            ("symbolname_view", 10),
            ("disp_total_score", 8),
            ("disp_buy_score", 6),
            ("disp_sell_score", 6),
            ("disp_final_score", 6),
            ("disp_slope", 4),
            ("disp_score_slope", 4),
            ("disp_mtf", 4),
            ("disp_score_mtf", 4),
            ("disp_rsi", 3),
            ("disp_macd", 3),
            ("disp_signal", 3),
            ("disp_base", 2),
            ("disp_trend", 2),
            ("disp_mom", 2),
            ("disp_vel", 2),
            ("disp_pen", 2),
            ("disp_close", 1),
            ("buy_reason_ja_view", 1),
            ("sell_reason_ja_view", 1),
            ("exit_reason_ja_view", 1),
            ("disp_ranking_type", 1),
            ("disp_ranking_rank", 1),
        ]:
            if c not in out.columns:
                continue
            try:
                if c in {"symbolname_view", "buy_reason_ja_view", "sell_reason_ja_view", "exit_reason_ja_view", "disp_ranking_type"}:
                    s = out[c].fillna("").astype(str).str.strip().ne("").astype(int)
                else:
                    s = pd.to_numeric(out[c], errors="coerce").notna().astype(int)
                complete_score += s * w
            except Exception:
                continue

        out["_complete_score"] = complete_score
        sort_cols = ["symbol", "_complete_score"]
        ascending = [True, False]

        if dt_col:
            try:
                out[dt_col] = pd.to_datetime(out[dt_col], errors="coerce")
                try:
                    out[dt_col] = out[dt_col].dt.tz_localize(None)
                except Exception:
                    pass
                sort_cols.append(dt_col)
                ascending.append(False)
            except Exception:
                logger.debug("[SUMMARY DISPLAY] dt sort normalize failed", exc_info=True)

        out = out.sort_values(sort_cols, ascending=ascending, na_position="last", kind="mergesort")
        out = out.drop_duplicates(subset=["symbol"], keep="first").copy()
        return out.drop(columns=["_complete_score"], errors="ignore").reset_index(drop=True)

    except Exception:
        logger.exception("[SUMMARY DISPLAY] dedupe_one_row_per_symbol failed")
        return safe_df(df)