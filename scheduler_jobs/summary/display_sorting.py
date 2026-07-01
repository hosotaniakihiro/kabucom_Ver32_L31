# ============================================================
# File   : scheduler_jobs/summary/display_sorting.py
# Function:
#   - BUY / SELL / AI BUY / AI SELL / AI EXIT の並び替え
#   - header helper
#   - TOP10表示前の対象外フィルタ
# ------------------------------------------------------------
# Version: Ver1.4-TOP10-SCORE-ONLY-DISPLAY-GUARD
# ------------------------------------------------------------
# ✔ BUY TOP10 は価格範囲 + buy_score > 0 で表示
# ✔ SELL TOP10 は価格範囲 + sell_score > 0 で表示
# ✔ slope条件は表示TOP10では使わない
# ✔ AI BUY / AI SELL は従来どおり AI通過フラグも見る
# ✔ エントリー判定は変更しない。表示専用の緩和。
# ============================================================

from __future__ import annotations

import logging
import os

import pandas as pd

from .time_utils import resolve_display_slot
from .display_normalizer import dedupe_one_row_per_symbol

logger = logging.getLogger(__name__)


# ============================================================
# display universe settings
# ============================================================

DEFAULT_DISPLAY_MIN_PRICE = 200.0
DEFAULT_DISPLAY_MAX_PRICE = 7000.0
DEFAULT_DISPLAY_MIN_BUY_SLOPE = 0.01
DEFAULT_DISPLAY_MAX_SELL_SLOPE = -0.01
DEFAULT_DISPLAY_MIN_BUY_SCORE = 0.000001
DEFAULT_DISPLAY_MIN_SELL_SCORE = 0.000001


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if raw in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _resolve_min_price() -> float:
    for name in (
        "SUMMARY_DISPLAY_MIN_PRICE",
        "TRADE_UNIVERSE_MIN_PRICE",
        "ENTRY_MIN_PRICE",
        "RANKING_MIN_PRICE",
    ):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, DEFAULT_DISPLAY_MIN_PRICE)
    return float(DEFAULT_DISPLAY_MIN_PRICE)


def _resolve_max_price() -> float:
    for name in (
        "SUMMARY_DISPLAY_MAX_PRICE",
        "TRADE_UNIVERSE_MAX_PRICE",
        "ENTRY_MAX_PRICE",
        "RANKING_MAX_PRICE",
    ):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, DEFAULT_DISPLAY_MAX_PRICE)
    return float(DEFAULT_DISPLAY_MAX_PRICE)


def _resolve_min_buy_slope() -> float:
    v1 = os.getenv("SUMMARY_DISPLAY_MIN_BUY_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MIN_BUY_SLOPE", DEFAULT_DISPLAY_MIN_BUY_SLOPE)
    v2 = os.getenv("ENTRY_MIN_BUY_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("ENTRY_MIN_BUY_SLOPE", DEFAULT_DISPLAY_MIN_BUY_SLOPE)
    return float(DEFAULT_DISPLAY_MIN_BUY_SLOPE)


def _resolve_max_sell_slope() -> float:
    v1 = os.getenv("SUMMARY_DISPLAY_MAX_SELL_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MAX_SELL_SLOPE", DEFAULT_DISPLAY_MAX_SELL_SLOPE)
    v2 = os.getenv("ENTRY_MAX_SELL_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("ENTRY_MAX_SELL_SLOPE", DEFAULT_DISPLAY_MAX_SELL_SLOPE)
    return float(DEFAULT_DISPLAY_MAX_SELL_SLOPE)


def _resolve_min_buy_score() -> float:
    return _env_float("SUMMARY_DISPLAY_MIN_BUY_SCORE", DEFAULT_DISPLAY_MIN_BUY_SCORE)


def _resolve_min_sell_score() -> float:
    return _env_float("SUMMARY_DISPLAY_MIN_SELL_SCORE", DEFAULT_DISPLAY_MIN_SELL_SCORE)


def _top10_slope_filter_enabled() -> bool:
    """TOP10表示だけのslope条件。既定OFF。

    エントリー判定は別ロジックで行う。ここは状態確認用のTOP10なので、
    score_buy / score_sell が出ている銘柄をまず見せる。
    旧挙動へ戻したい場合だけ SUMMARY_DISPLAY_TOP10_REQUIRE_SLOPE=1。
    """
    return _env_bool("SUMMARY_DISPLAY_TOP10_REQUIRE_SLOPE", False)


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce").fillna(default)
    except Exception:
        pass
    return pd.Series(default, index=df.index, dtype="float64")


def _select_price_series(df: pd.DataFrame) -> pd.Series:
    for c in (
        "disp_close",
        "close",
        "close_price",
        "current_price",
        "price",
        "last_price",
    ):
        if c in df.columns:
            return _num(df, c, 0.0)
    return pd.Series(0.0, index=df.index, dtype="float64")


def _select_slope_series(df: pd.DataFrame) -> pd.Series:
    for c in (
        "disp_slope",
        "slope",
        "score_slope",
        "slope_atr_scaled",
        "ma75_slope",
    ):
        if c in df.columns:
            return _num(df, c, 0.0)
    return pd.Series(0.0, index=df.index, dtype="float64")


def _select_buy_score_series(df: pd.DataFrame) -> pd.Series:
    for c in (
        "disp_buy_score",
        "buy_score",
        "score_buy",
        "ai_buy_score",
    ):
        if c in df.columns:
            return _num(df, c, 0.0)
    return pd.Series(0.0, index=df.index, dtype="float64")


def _select_sell_score_series(df: pd.DataFrame) -> pd.Series:
    for c in (
        "disp_sell_score",
        "sell_score",
        "score_sell",
        "ai_sell_score",
    ):
        if c in df.columns:
            return _num(df, c, 0.0).abs()
    return pd.Series(0.0, index=df.index, dtype="float64")


def _apply_price_cap(price_s: pd.Series, min_price: float, max_price: float) -> pd.Series:
    mask = price_s > float(min_price)
    if float(max_price) > 0:
        mask &= price_s <= float(max_price)
    return mask


def _price_condition_text(min_price: float, max_price: float) -> str:
    if float(max_price) > 0:
        return f"{float(min_price):.1f} < close <= {float(max_price):.1f}"
    return f"close > {float(min_price):.1f}"


def _apply_buy_display_guard(df: pd.DataFrame) -> pd.DataFrame:
    """BUY TOP10 / AI BUY 表示前フィルタ。

    既定条件:
      min_price < close <= max_price
      buy_score > min_buy_score

    slope条件は表示TOP10では既定OFF。
    """
    if df is None or df.empty:
        return df

    out = df.copy()
    min_price = _resolve_min_price()
    max_price = _resolve_max_price()
    min_slope = _resolve_min_buy_slope()
    min_buy_score = _resolve_min_buy_score()
    require_slope = _top10_slope_filter_enabled()

    price_s = _select_price_series(out)
    slope_s = _select_slope_series(out)
    buy_s = _select_buy_score_series(out)

    before = len(out)
    price_mask = _apply_price_cap(price_s, min_price, max_price)
    score_mask = buy_s > float(min_buy_score)
    slope_mask = slope_s > float(min_slope)

    keep_mask = price_mask & score_mask
    if require_slope:
        keep_mask &= slope_mask

    out = out[keep_mask].copy()

    logger.info(
        "[SUMMARY DISPLAY SORTING] BUY guard condition='%s and buy_score > %.6f%s' "
        "before=%s after=%s skipped=%s price_ok=%s buy_score_nonzero=%s slope_ok=%s slope_required=%s",
        _price_condition_text(min_price, max_price),
        float(min_buy_score),
        f" and slope > {float(min_slope):.4f}" if require_slope else "",
        before,
        len(out),
        before - len(out),
        int(price_mask.sum()),
        int(score_mask.sum()),
        int(slope_mask.sum()),
        require_slope,
    )

    return out.reset_index(drop=True)


def _apply_sell_display_guard(df: pd.DataFrame) -> pd.DataFrame:
    """SELL TOP10 / AI SELL 表示前フィルタ。

    既定条件:
      min_price < close <= max_price
      sell_score > min_sell_score

    slope条件は表示TOP10では既定OFF。
    """
    if df is None or df.empty:
        return df

    out = df.copy()
    min_price = _resolve_min_price()
    max_price = _resolve_max_price()
    max_slope = _resolve_max_sell_slope()
    min_sell_score = _resolve_min_sell_score()
    require_slope = _top10_slope_filter_enabled()

    price_s = _select_price_series(out)
    slope_s = _select_slope_series(out)
    sell_s = _select_sell_score_series(out)

    before = len(out)
    price_mask = _apply_price_cap(price_s, min_price, max_price)
    score_mask = sell_s > float(min_sell_score)
    slope_mask = slope_s < float(max_slope)

    keep_mask = price_mask & score_mask
    if require_slope:
        keep_mask &= slope_mask

    out = out[keep_mask].copy()

    logger.info(
        "[SUMMARY DISPLAY SORTING] SELL guard condition='%s and sell_score > %.6f%s' "
        "before=%s after=%s skipped=%s price_ok=%s sell_score_nonzero=%s slope_ok=%s slope_required=%s",
        _price_condition_text(min_price, max_price),
        float(min_sell_score),
        f" and slope < {float(max_slope):.4f}" if require_slope else "",
        before,
        len(out),
        before - len(out),
        int(price_mask.sum()),
        int(score_mask.sum()),
        int(slope_mask.sum()),
        require_slope,
    )

    return out.reset_index(drop=True)


# ============================================================
# header helpers
# ============================================================

def latest_header_text(df: pd.DataFrame, title_label: str) -> str | None:
    try:
        if df is None or df.empty:
            return None

        dt_col = None
        for c in ("datetime", "end_time", "start_time"):
            if c in df.columns:
                dt_col = c
                break
        if dt_col is None:
            return None

        s = pd.to_datetime(df[dt_col], errors="coerce").dropna()
        if s.empty:
            return None

        latest_dt = s.max()
        return f"=== ⏱ 最新 {title_label}｜{latest_dt} ==="
    except Exception:
        logger.debug("[SUMMARY DISPLAY] latest header build failed", exc_info=True)
        return None


def build_header_context(df: pd.DataFrame, interval_label: str) -> str:
    try:
        slot = resolve_display_slot()
    except Exception:
        slot = None

    latest = latest_header_text(df, f"{interval_label} サマリー")
    if latest and slot:
        return f"{latest} [{slot}]"
    if latest:
        return latest
    if slot:
        return f"=== ⏱ 最新 {interval_label} サマリー｜[{slot}] ==="
    return f"=== ⏱ 最新 {interval_label} サマリー ==="


# ============================================================
# TOP10 sorting
# ============================================================

def prepare_buy_df(df: pd.DataFrame) -> pd.DataFrame:
    """BUY TOP10用。価格範囲 + buy_score 正で抽出し、スコア順に並べる。"""
    df = dedupe_one_row_per_symbol(df)
    if df.empty:
        return df

    df = _apply_buy_display_guard(df)
    if df.empty:
        return df

    return df.sort_values(
        by=["disp_buy_score", "disp_total_score", "disp_mtf", "disp_slope"],
        ascending=[False, False, False, False],
        na_position="last",
        kind="mergesort",
    ).reset_index(drop=True)


def prepare_sell_df(df: pd.DataFrame) -> pd.DataFrame:
    """SELL TOP10用。価格範囲 + sell_score 正で抽出し、スコア順に並べる。"""
    df = dedupe_one_row_per_symbol(df)
    if df.empty:
        return df

    df = _apply_sell_display_guard(df)
    if df.empty:
        return df

    tech_quality = pd.Series(0, index=df.index, dtype="int64")
    for col in ("disp_rsi", "disp_macd", "disp_signal"):
        try:
            s = pd.to_numeric(df[col], errors="coerce")
            tech_quality += s.notna().astype(int) * 2
        except Exception:
            continue

    try:
        rsi0 = pd.to_numeric(df["disp_rsi"], errors="coerce").fillna(0).eq(0)
        macd0 = pd.to_numeric(df["disp_macd"], errors="coerce").fillna(0).eq(0)
        signal0 = pd.to_numeric(df["disp_signal"], errors="coerce").fillna(0).eq(0)
        all_zero_tech = rsi0 & macd0 & signal0
        tech_quality -= all_zero_tech.astype(int) * 3
    except Exception:
        pass

    df = df.copy()
    df["_tech_quality"] = tech_quality

    df = df.sort_values(
        by=["disp_sell_score", "_tech_quality", "disp_mtf", "disp_slope"],
        ascending=[False, False, True, True],
        na_position="last",
        kind="mergesort",
    )

    return df.drop(columns=["_tech_quality"], errors="ignore").reset_index(drop=True)


# ============================================================
# AI passed sorting
# ============================================================

def prepare_ai_buy_df(df: pd.DataFrame) -> pd.DataFrame:
    out = prepare_buy_df(df)
    if out.empty:
        return out
    try:
        return out[out["ai_buy_passed_view"].fillna(False)].copy().reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def prepare_ai_sell_df(df: pd.DataFrame) -> pd.DataFrame:
    out = prepare_sell_df(df)
    if out.empty:
        return out
    try:
        return out[out["ai_sell_passed_view"].fillna(False)].copy().reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def prepare_ai_exit_df(df: pd.DataFrame) -> pd.DataFrame:
    df = dedupe_one_row_per_symbol(df)
    if df.empty:
        return df

    try:
        out = df[df["ai_exit_passed_view"].fillna(False)].copy()
    except Exception:
        return pd.DataFrame()

    if out.empty:
        return out

    try:
        return out.sort_values(
            by=["disp_final_score", "disp_total_score", "disp_mtf", "disp_slope"],
            ascending=[True, True, True, True],
            na_position="last",
            kind="mergesort",
        ).reset_index(drop=True)
    except Exception:
        return out.reset_index(drop=True)


__all__ = [
    "latest_header_text",
    "build_header_context",
    "prepare_buy_df",
    "prepare_sell_df",
    "prepare_ai_buy_df",
    "prepare_ai_sell_df",
    "prepare_ai_exit_df",
]
