# ============================================================
# File   : trading/summary/pipeline/indicator_pipeline.py
# Version: Ver42.5-PRODUCTION-MODULAR-INDICATOR-PIPELINE
#          -MTF-NAN-PRESERVE
#          -READINESS-VISIBLE
#          -ZERO-AND-NAN-SEPARATED
# ------------------------------------------------------------
# 【概要】
#   summary DF に対して indicator / scoring を適用する。
#
# 【今回の修正】
#   - mtf / score_mtf を 0 埋めしない
#   - technical_ready が False / 未計算の状態を明確に保持
#   - readiness 内訳ログを追加
#   - downstream scoring 後も mtf / slope の NaN を極力 preserve
#   - ready_rows=0 の原因が追えるようログ強化
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# optional imports (existing project compatibility)
# ============================================================

try:
    from trading.scoring.scoring_core import (
        calc_buy_score,
        calc_sell_score,
    )
except Exception:  # pragma: no cover
    calc_buy_score = None
    calc_sell_score = None

try:
    from trading.scoring.core.scoring_pipeline import scoring_pipeline
except Exception:  # pragma: no cover
    scoring_pipeline = None

try:
    from trading.summary.indicators.indicator_calculator import add_all_indicators as calculate_indicators
except Exception:
    calculate_indicators = None
    logger.warning("[indicator_pipeline] calculate_indicators import unavailable -> skipped", exc_info=True)


# ============================================================
# helpers
# ============================================================

def _as_numeric_series(
    df: pd.DataFrame,
    col: str,
    default: float = 0.0,
    *,
    fillna: bool = True,
) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    s = pd.to_numeric(df[col], errors="coerce")
    if fillna:
        s = s.fillna(default)
    return s


def _as_numeric_nan_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def _pick_first_numeric(df: pd.DataFrame, candidates: tuple[str, ...]) -> pd.Series:
    base = pd.Series(np.nan, index=df.index, dtype="float64")
    for c in candidates:
        if c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce")
            try:
                base = base.combine_first(s)
            except Exception:
                base = base.where(base.notna(), s)
    return base


def _safe_nonzero_count(df: pd.DataFrame, col: str) -> int:
    if col not in df.columns:
        return 0
    s = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return int((s != 0).sum())


def _safe_nonnull_count(df: pd.DataFrame, col: str) -> int:
    if col not in df.columns:
        return 0
    s = pd.to_numeric(df[col], errors="coerce")
    return int(s.notna().sum())


def _safe_symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "datetime" not in df.columns:
            return None
        return pd.to_datetime(df["datetime"], errors="coerce").max()
    except Exception:
        return None


def _log_column_health(df: pd.DataFrame, stage: str, interval: Optional[int], col: str) -> None:
    if col not in df.columns:
        logger.info(
            "[indicator_pipeline] %s interval=%s col=%s missing",
            stage,
            interval,
            col,
        )
        return

    if col == "technical_ready":
        try:
            s = pd.Series(df[col]).fillna(False).astype(bool)
            logger.info(
                "[indicator_pipeline] %s interval=%s col=%s non_null=%d nonzero=%d nunique=%d min=%s max=%s",
                stage,
                interval,
                col,
                int(s.notna().sum()),
                int(s.sum()),
                int(s.nunique(dropna=True)),
                s.min() if not s.empty else None,
                s.max() if not s.empty else None,
            )
            return
        except Exception:
            logger.exception("[indicator_pipeline] technical_ready log failed stage=%s interval=%s", stage, interval)
            return

    s = pd.to_numeric(df[col], errors="coerce")
    logger.info(
        "[indicator_pipeline] %s interval=%s col=%s non_null=%d nonzero=%d nunique=%d min=%s max=%s",
        stage,
        interval,
        col,
        int(s.notna().sum()),
        int((s.fillna(0.0) != 0).sum()),
        int(s.nunique(dropna=True)),
        s.min(),
        s.max(),
    )


def _log_readiness_detail(df: pd.DataFrame, stage: str, interval: Optional[int]) -> None:
    detail_cols = (
        "close",
        "atr",
        "slope",
        "score_slope",
        "mtf",
        "score_mtf",
        "rsi",
        "macd",
        "signal",
        "score_total",
        "final_score",
        "technical_ready",
    )
    for c in detail_cols:
        _log_column_health(df, f"{stage}-readiness", interval, c)


def _log_summary(df: pd.DataFrame, stage: str, interval: Optional[int]) -> None:
    if not isinstance(df, pd.DataFrame):
        logger.warning("[indicator_pipeline] %s interval=%s input is not DataFrame", stage, interval)
        return

    ready_rows = 0
    if "technical_ready" in df.columns:
        try:
            ready_rows = int(pd.Series(df["technical_ready"]).fillna(False).astype(bool).sum())
        except Exception:
            ready_rows = 0

    logger.info(
        "[indicator_pipeline] %s interval=%s rows=%d cols=%d symbols=%d latest_dt=%s ready_rows=%d",
        stage,
        interval,
        len(df),
        len(df.columns),
        _safe_symbol_count(df),
        _latest_dt(df),
        ready_rows,
    )

    for c in (
        "open", "high", "low", "close", "volume",
        "atr", "slope_atr_scaled", "ma25", "ma75",
        "score_buy", "score_sell", "score_slope",
        "score_mtf", "score_total", "rsi", "macd", "signal",
        "technical_ready", "slope", "mtf",
    ):
        _log_column_health(df, stage, interval, c)

    if stage in ("after-main", "after-scoring", "final"):
        _log_readiness_detail(df, stage, interval)


def _clip_non_negative(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0.0).clip(lower=0.0)


def _sanitize_price_like(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    s = s.mask(s <= 0, np.nan)
    return s


def _normalize_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")

    if "symbol" in out.columns and "datetime" in out.columns:
        out = out.dropna(subset=["symbol", "datetime"]).copy()
        out = (
            out.sort_values(["symbol", "datetime"], kind="mergesort")
               .drop_duplicates(["symbol", "datetime"], keep="last")
               .reset_index(drop=True)
        )

    return out


def _ensure_base_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "symbol" not in out.columns:
        out["symbol"] = ""

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "date" in out.columns and "start_time" in out.columns:
        out["datetime"] = pd.to_datetime(
            out["date"].astype(str).str.strip() + " " + out["start_time"].astype(str).str.strip(),
            errors="coerce",
        )
    else:
        out["datetime"] = pd.NaT

    close_src = _pick_first_numeric(
        out,
        (
            "close",
            "close_price",
            "closevalue",
            "closeValue",
            "price",
            "current_price",
            "CurrentPrice",
            "last_price",
            "LastPrice",
            "現在値",
            "終値",
        ),
    )
    close_src = _sanitize_price_like(close_src)

    open_src = _pick_first_numeric(out, ("open", "open_price", "openvalue", "openValue", "始値"))
    high_src = _pick_first_numeric(out, ("high", "high_price", "highvalue", "highValue", "高値"))
    low_src = _pick_first_numeric(out, ("low", "low_price", "lowvalue", "lowValue", "安値"))

    open_src = _sanitize_price_like(open_src).combine_first(close_src)
    high_src = _sanitize_price_like(high_src).combine_first(close_src)
    low_src = _sanitize_price_like(low_src).combine_first(close_src)

    volume_src = _pick_first_numeric(
        out,
        ("volume", "trading_volume", "TradingVolume", "出来高", "volume_total"),
    )
    volume_src = pd.to_numeric(volume_src, errors="coerce").replace([np.inf, -np.inf], np.nan)

    out["open"] = open_src
    out["high"] = high_src
    out["low"] = low_src
    out["close"] = close_src
    out["volume"] = volume_src.fillna(0.0)

    out["open_price"] = _pick_first_numeric(out, ("open_price", "open")).combine_first(out["open"])
    out["high_price"] = _pick_first_numeric(out, ("high_price", "high")).combine_first(out["high"])
    out["low_price"] = _pick_first_numeric(out, ("low_price", "low")).combine_first(out["low"])
    out["close_price"] = _pick_first_numeric(out, ("close_price", "close", "price")).combine_first(out["close"])

    if "price" not in out.columns:
        out["price"] = out["close_price"]
    else:
        out["price"] = pd.to_numeric(out["price"], errors="coerce").combine_first(out["close_price"])

    if "current_price" not in out.columns:
        out["current_price"] = out["close_price"]

    for c in ("score_buy", "score_sell", "score_total", "score", "final_score", "display_score", "combined_score"):
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    # 未計算とゼロを区別したい列は NaN を保持
    for c in ("score_slope", "score_mtf", "slope", "mtf", "slope_atr_scaled", "atr", "rsi", "macd", "signal", "hist"):
        if c not in out.columns:
            out[c] = np.nan
        out[c] = pd.to_numeric(out[c], errors="coerce")

    if "technical_ready" not in out.columns:
        out["technical_ready"] = False
    else:
        out["technical_ready"] = pd.Series(out["technical_ready"]).fillna(False).astype(bool)

    out = _normalize_symbol_datetime(out)
    return out


def _apply_indicator_calculation(df: pd.DataFrame, interval: Optional[int]) -> pd.DataFrame:
    if calculate_indicators is None:
        logger.warning("[indicator_pipeline] calculate_indicators import unavailable -> skipped")
        return df

    try:
        out = calculate_indicators(df, interval=interval)
        if isinstance(out, pd.DataFrame):
            return out
        logger.warning("[indicator_pipeline] calculate_indicators returned non-DataFrame -> input kept")
        return df
    except TypeError:
        try:
            out = calculate_indicators(df)
            if isinstance(out, pd.DataFrame):
                return out
        except Exception:
            logger.exception("[indicator_pipeline] calculate_indicators failed (legacy signature)")
        return df
    except Exception:
        logger.exception("[indicator_pipeline] calculate_indicators failed")
        return df


def _apply_scoring_core(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    prev_buy = _clip_non_negative(_as_numeric_series(out, "score_buy"))
    prev_sell = _clip_non_negative(_as_numeric_series(out, "score_sell"))

    if calc_buy_score is not None:
        try:
            result = calc_buy_score(out)
            if isinstance(result, pd.Series):
                out["score_buy"] = pd.to_numeric(result, errors="coerce").fillna(prev_buy)
            elif isinstance(result, pd.DataFrame) and "score_buy" in result.columns:
                out["score_buy"] = pd.to_numeric(result["score_buy"], errors="coerce").fillna(prev_buy)
            else:
                out["score_buy"] = prev_buy
        except Exception:
            logger.exception("[indicator_pipeline] calc_buy_score failed -> preserve previous score_buy")
            out["score_buy"] = prev_buy
    else:
        out["score_buy"] = prev_buy

    if calc_sell_score is not None:
        try:
            result = calc_sell_score(out)
            if isinstance(result, pd.Series):
                out["score_sell"] = pd.to_numeric(result, errors="coerce").fillna(prev_sell)
            elif isinstance(result, pd.DataFrame) and "score_sell" in result.columns:
                out["score_sell"] = pd.to_numeric(result["score_sell"], errors="coerce").fillna(prev_sell)
            else:
                out["score_sell"] = prev_sell
        except Exception:
            logger.exception("[indicator_pipeline] calc_sell_score failed -> preserve previous score_sell")
            out["score_sell"] = prev_sell
    else:
        out["score_sell"] = prev_sell

    out["score_buy"] = _clip_non_negative(out["score_buy"])
    out["score_sell"] = _clip_non_negative(out["score_sell"])
    out["buy_score"] = out["score_buy"]
    out["sell_score"] = out["score_sell"]

    return out


def _ensure_slope_and_mtf(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    slope_src = (
        pd.to_numeric(out["slope_atr_scaled"], errors="coerce")
        if "slope_atr_scaled" in out.columns
        else pd.Series(np.nan, index=out.index, dtype="float64")
    )
    existing_slope = (
        pd.to_numeric(out["slope"], errors="coerce")
        if "slope" in out.columns
        else pd.Series(np.nan, index=out.index, dtype="float64")
    )

    out["slope"] = existing_slope.combine_first(slope_src)

    existing_score_slope = (
        pd.to_numeric(out["score_slope"], errors="coerce")
        if "score_slope" in out.columns
        else pd.Series(np.nan, index=out.index, dtype="float64")
    )
    out["score_slope"] = existing_score_slope.combine_first(out["slope"])

    # mtf はこの pipeline では新規計算しない
    # 既存列を優先し、未計算は NaN のまま保持する
    existing_mtf = (
        pd.to_numeric(out["mtf"], errors="coerce")
        if "mtf" in out.columns
        else pd.Series(np.nan, index=out.index, dtype="float64")
    )
    existing_score_mtf = (
        pd.to_numeric(out["score_mtf"], errors="coerce")
        if "score_mtf" in out.columns
        else pd.Series(np.nan, index=out.index, dtype="float64")
    )

    out["mtf"] = existing_mtf.combine_first(existing_score_mtf)
    out["score_mtf"] = existing_score_mtf.combine_first(existing_mtf)

    return out


def _compose_total_score(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    buy = _clip_non_negative(_as_numeric_series(out, "score_buy"))
    sell = _clip_non_negative(_as_numeric_series(out, "score_sell"))

    slope_raw = (
        pd.to_numeric(out["score_slope"], errors="coerce")
        if "score_slope" in out.columns
        else pd.Series(np.nan, index=out.index, dtype="float64")
    )
    mtf_raw = (
        pd.to_numeric(out["score_mtf"], errors="coerce")
        if "score_mtf" in out.columns
        else pd.Series(np.nan, index=out.index, dtype="float64")
    )

    total_existing = _as_numeric_series(out, "score_total")
    slope_f = slope_raw.fillna(0.0)
    mtf_f = mtf_raw.fillna(0.0)

    synth_total = buy - sell + slope_f + mtf_f
    out["score_total"] = total_existing.where(total_existing != 0, synth_total)

    score_existing = _as_numeric_series(out, "score")
    out["score"] = score_existing.where(score_existing != 0, out["score_total"])

    final_existing = _as_numeric_series(out, "final_score")
    out["final_score"] = final_existing.where(final_existing != 0, out["score_total"])

    combined_existing = _as_numeric_series(out, "combined_score")
    out["combined_score"] = combined_existing.where(combined_existing != 0, out["score_total"])

    display_existing = _as_numeric_series(out, "display_score")
    out["display_score"] = display_existing.where(display_existing != 0, out["score_total"])

    out["buy_score"] = buy
    out["sell_score"] = sell

    mtf_existing = _as_numeric_series(out, "mtf_score")
    out["mtf_score"] = mtf_existing.where(mtf_existing != 0, mtf_f)

    base_existing = _as_numeric_series(out, "base_score")
    directional_base = buy - sell + slope_f
    out["base_score"] = base_existing.where(base_existing != 0, directional_base)

    out["score_buy"] = buy
    out["score_sell"] = sell

    out["score_total"] = pd.to_numeric(out["score_total"], errors="coerce").fillna(0.0)
    out["score"] = pd.to_numeric(out["score"], errors="coerce").fillna(0.0)
    out["final_score"] = pd.to_numeric(out["final_score"], errors="coerce").fillna(0.0)
    out["display_score"] = pd.to_numeric(out["display_score"], errors="coerce").fillna(0.0)
    out["combined_score"] = pd.to_numeric(out["combined_score"], errors="coerce").fillna(0.0)

    try:
        ready_s = (
            pd.Series(out["technical_ready"]).fillna(False).astype(bool)
            if "technical_ready" in out.columns
            else pd.Series(False, index=out.index)
        )
        total_s = pd.to_numeric(out["score_total"], errors="coerce").fillna(0.0)

        slope_visible = (
            pd.to_numeric(out["slope"], errors="coerce")
            if "slope" in out.columns
            else pd.Series(np.nan, index=out.index)
        )
        rsi_raw = (
            pd.to_numeric(out["rsi"], errors="coerce")
            if "rsi" in out.columns
            else pd.Series(np.nan, index=out.index)
        )
        volume_raw = (
            pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
            if "volume" in out.columns
            else pd.Series(0.0, index=out.index)
        )

        rsi_bias = pd.Series(0.0, index=out.index)
        rsi_bias = rsi_bias.where(~rsi_raw.notna(), (rsi_raw.fillna(50.0) - 50.0) / 10.0)

        vol_bias = pd.Series(0.0, index=out.index)
        try:
            vol_bias = np.log1p(volume_raw.clip(lower=0.0)) / 10.0
        except Exception:
            pass

        fallback_total = buy - sell + slope_f + rsi_bias + vol_bias

        use_fallback = (~ready_s) & (total_s == 0) & (
            slope_visible.notna() | rsi_raw.notna() | volume_raw.gt(0)
        )

        out.loc[use_fallback, "score_total"] = fallback_total.loc[use_fallback]
        out.loc[use_fallback, "score"] = fallback_total.loc[use_fallback]
        out.loc[use_fallback, "final_score"] = fallback_total.loc[use_fallback]
        out.loc[use_fallback, "combined_score"] = fallback_total.loc[use_fallback]
        out.loc[use_fallback, "display_score"] = fallback_total.loc[use_fallback]

    except Exception:
        logger.exception("[indicator_pipeline] immature fallback total score failed")

    out["score_total"] = pd.to_numeric(out["score_total"], errors="coerce").fillna(0.0)
    out["score"] = pd.to_numeric(out["score"], errors="coerce").fillna(0.0)
    out["final_score"] = pd.to_numeric(out["final_score"], errors="coerce").fillna(0.0)
    out["display_score"] = pd.to_numeric(out["display_score"], errors="coerce").fillna(0.0)
    out["combined_score"] = pd.to_numeric(out["combined_score"], errors="coerce").fillna(0.0)

    # テクニカル系は NaN preserve
    for c in ("score_slope", "score_mtf", "slope", "mtf", "rsi", "macd", "signal", "hist"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    return out


def _canonicalize_after_downstream(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    buy = _clip_non_negative(_as_numeric_series(out, "score_buy"))
    sell = _clip_non_negative(_as_numeric_series(out, "score_sell"))

    total = _as_numeric_series(out, "score_total")
    score_col = _as_numeric_series(out, "score")
    final_col = _as_numeric_series(out, "final_score")
    combined_col = _as_numeric_series(out, "combined_score")

    if int((total != 0).sum()) == 0:
        total = score_col
    if int((total != 0).sum()) == 0:
        total = final_col
    if int((total != 0).sum()) == 0:
        total = combined_col
    if int((total != 0).sum()) == 0:
        total = buy - sell + _as_numeric_series(out, "score_slope") + _as_numeric_series(out, "score_mtf")

    out["score_buy"] = buy
    out["score_sell"] = sell
    out["buy_score"] = buy
    out["sell_score"] = sell

    out["score_total"] = total
    out["score"] = score_col.where(score_col != 0, total)
    out["final_score"] = final_col.where(final_col != 0, total)
    out["combined_score"] = combined_col.where(combined_col != 0, total)

    display_existing = _as_numeric_series(out, "display_score")
    out["display_score"] = display_existing.where(display_existing != 0, out["score_total"])

    if "mtf_score" not in out.columns:
        out["mtf_score"] = _as_numeric_series(out, "score_mtf")
    else:
        out["mtf_score"] = _as_numeric_series(out, "mtf_score").where(
            _as_numeric_series(out, "mtf_score") != 0,
            _as_numeric_series(out, "score_mtf"),
        )

    if "base_score" not in out.columns:
        out["base_score"] = buy - sell + _as_numeric_series(out, "score_slope")
    else:
        out["base_score"] = _as_numeric_series(out, "base_score").where(
            _as_numeric_series(out, "base_score") != 0,
            buy - sell + _as_numeric_series(out, "score_slope"),
        )

    out["score_total"] = pd.to_numeric(out["score_total"], errors="coerce").fillna(0.0)
    out["score"] = pd.to_numeric(out["score"], errors="coerce").fillna(0.0)
    out["final_score"] = pd.to_numeric(out["final_score"], errors="coerce").fillna(0.0)
    out["combined_score"] = pd.to_numeric(out["combined_score"], errors="coerce").fillna(0.0)
    out["display_score"] = pd.to_numeric(out["display_score"], errors="coerce").fillna(0.0)

    for c in ("score_slope", "score_mtf", "slope", "mtf", "rsi", "macd", "signal", "hist"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    if "technical_ready" in out.columns:
        out["technical_ready"] = pd.Series(out["technical_ready"]).fillna(False).astype(bool)

    return out


def _maybe_run_downstream_scoring_pipeline(
    df: pd.DataFrame,
    interval: Optional[int],
) -> pd.DataFrame:
    if scoring_pipeline is None:
        logger.warning("[indicator_pipeline] scoring_pipeline import unavailable -> skipped")
        return df

    interval_label = None if interval is None else f"{interval}m"

    try:
        before_mtf = pd.to_numeric(df["mtf"], errors="coerce") if "mtf" in df.columns else None
        before_score_mtf = pd.to_numeric(df["score_mtf"], errors="coerce") if "score_mtf" in df.columns else None

        out = scoring_pipeline(
            df,
            interval=interval_label,
            apply_output_filter=False,
        )
        if not isinstance(out, pd.DataFrame):
            logger.warning("[indicator_pipeline] scoring_pipeline returned non-DataFrame -> input kept")
            return df

        # downstream が mtf を 0 で埋めても、既存の NaN/値を優先復元
        if before_mtf is not None:
            after_mtf = pd.to_numeric(out["mtf"], errors="coerce") if "mtf" in out.columns else pd.Series(np.nan, index=out.index)
            out["mtf"] = before_mtf.reindex(out.index).combine_first(after_mtf)

        if before_score_mtf is not None:
            after_score_mtf = pd.to_numeric(out["score_mtf"], errors="coerce") if "score_mtf" in out.columns else pd.Series(np.nan, index=out.index)
            out["score_mtf"] = before_score_mtf.reindex(out.index).combine_first(after_score_mtf)

        return out

    except TypeError:
        try:
            out = scoring_pipeline(df, interval=interval_label)
            if isinstance(out, pd.DataFrame):
                return out
        except Exception:
            logger.exception("[indicator_pipeline] scoring_pipeline failed (legacy signature)")
        return df
    except Exception:
        logger.exception("[indicator_pipeline] scoring_pipeline failed")
        return df


# ============================================================
# public
# ============================================================

def run_indicator_pipeline(
    df: pd.DataFrame,
    interval: Optional[int] = None,
    run_downstream_scoring: bool = True,
) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        logger.warning("[indicator_pipeline] input is not DataFrame")
        return df

    if df.empty:
        logger.info("[indicator_pipeline] empty input")
        return df.copy()

    out = df.copy()

    out = _ensure_base_columns(out)
    _log_summary(out, "input", interval)

    out = _apply_indicator_calculation(out, interval)
    out = _ensure_base_columns(out)
    _log_summary(out, "after-main", interval)

    # mtf が全部 0 なら本来未計算の可能性が高いため warning
    try:
        if "mtf" in out.columns:
            mtf_s = pd.to_numeric(out["mtf"], errors="coerce")
            if int(mtf_s.notna().sum()) > 0 and int((mtf_s.fillna(0.0) != 0).sum()) == 0:
                logger.warning(
                    "[indicator_pipeline] mtf all-zero-or-null interval=%s rows=%s latest_dt=%s",
                    interval,
                    len(out),
                    _latest_dt(out),
                )
    except Exception:
        logger.exception("[indicator_pipeline] mtf health check failed interval=%s", interval)

    out = _apply_scoring_core(out)
    out = _ensure_slope_and_mtf(out)
    out = _compose_total_score(out)
    _log_summary(out, "after-scoring", interval)

    if run_downstream_scoring:
        out = _maybe_run_downstream_scoring_pipeline(out, interval)

    out = _ensure_base_columns(out)
    out = _ensure_slope_and_mtf(out)
    out = _compose_total_score(out)
    out = _canonicalize_after_downstream(out)
    out = _normalize_symbol_datetime(out)
    _log_summary(out, "final", interval)

    logger.info(
        "[indicator_pipeline] interval=%s rows=%d cols=%d symbols=%d latest_dt=%s columns=%s",
        interval,
        len(out),
        len(out.columns),
        _safe_symbol_count(out),
        _latest_dt(out),
        list(out.columns),
    )

    return out


def indicator_pipeline(
    df: pd.DataFrame,
    interval: Optional[int] = None,
    run_downstream_scoring: bool = True,
) -> pd.DataFrame:
    return run_indicator_pipeline(
        df=df,
        interval=interval,
        run_downstream_scoring=run_downstream_scoring,
    )


def apply_indicator_pipeline(
    df: pd.DataFrame,
    interval: Optional[int] = None,
    run_downstream_scoring: bool = True,
) -> pd.DataFrame:
    return run_indicator_pipeline(
        df=df,
        interval=interval,
        run_downstream_scoring=run_downstream_scoring,
    )