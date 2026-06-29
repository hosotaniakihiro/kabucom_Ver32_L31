# ============================================================
# File   : scheduler_jobs/summary/display_sorting.py
# Function:
#   - BUY / SELL / AI BUY / AI SELL / AI EXIT の並び替え
#   - header helper
#   - TOP10表示前の対象外フィルタ
# ------------------------------------------------------------
# Version: Ver1.3-PRODUCTION-DISPLAY-PRICE-CAP
# ------------------------------------------------------------
# ✔ BUY TOP10 は buy_score / score_buy が正の銘柄だけ表示
# ✔ SELL TOP10 は sell_score / score_sell が正の銘柄だけ表示
# ✔ score_buy=0 の SELL 銘柄が BUY TOP10 に混ざる問題を修正
# ✔ score_sell=0 の BUY 銘柄が SELL TOP10 に混ざる問題を修正
# ✔ slope 環境変数を緩めても BUY/SELL の銘柄が同じにならない
# ✔ 表示・AI候補の価格上限を ENTRY / TRADE_UNIVERSE と共通化
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


def _resolve_min_price() -> float:
    """
    表示・候補共通の最低株価。

    close <= 200 を対象外にするため、判定は close > min_price。
    """
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
    """
    表示・AI候補共通の最高株価。

    エントリー側の上限とズレると、発注対象外の高価格銘柄が
    SUMMARY TOP10 / AI PASSED に表示されるため、ここでも同じ上限を使う。
    0 以下を指定した場合だけ上限無効として扱う。
    """
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
    """
    BUY対象の最低slope。
    環境変数で緩められても、BUY/SELL分離は score_buy 側で担保する。
    """
    v1 = os.getenv("SUMMARY_DISPLAY_MIN_BUY_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MIN_BUY_SLOPE", DEFAULT_DISPLAY_MIN_BUY_SLOPE)

    v2 = os.getenv("ENTRY_MIN_BUY_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("ENTRY_MIN_BUY_SLOPE", DEFAULT_DISPLAY_MIN_BUY_SLOPE)

    return float(DEFAULT_DISPLAY_MIN_BUY_SLOPE)


def _resolve_max_sell_slope() -> float:
    """
    SELL対象の最大slope。
    環境変数で緩められても、BUY/SELL分離は score_sell 側で担保する。
    """
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
    """
    BUY表示に使う実スコア。
    display_normalizer 後は disp_buy_score を優先する。
    """
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
    """
    SELL表示に使う実スコア。
    display_normalizer 後は disp_sell_score を優先する。
    """
    for c in (
        "disp_sell_score",
        "sell_score",
        "score_sell",
        "ai_sell_score",
    ):
        if c in df.columns:
            return _num(df, c, 0.0)
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
    """
    BUY TOP10 / AI BUY 表示前フィルタ。

    条件:
      min_price < close <= max_price
      slope > min_buy_slope
      buy_score > 0

    重要:
      slope条件を環境変数で -999 まで緩めても、buy_score=0 の
      SELL銘柄はBUY TOP10に入れない。
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    min_price = _resolve_min_price()
    max_price = _resolve_max_price()
    min_slope = _resolve_min_buy_slope()
    min_buy_score = _resolve_min_buy_score()

    price_s = _select_price_series(out)
    slope_s = _select_slope_series(out)
    buy_s = _select_buy_score_series(out)

    before = len(out)
    price_mask = _apply_price_cap(price_s, min_price, max_price)

    out = out[
        price_mask
        & (slope_s > float(min_slope))
        & (buy_s > float(min_buy_score))
    ].copy()

    logger.info(
        "[SUMMARY DISPLAY SORTING] BUY guard condition='%s and slope > %.4f and buy_score > %.6f' "
        "before=%s after=%s skipped=%s price_ok=%s buy_score_nonzero=%s",
        _price_condition_text(min_price, max_price),
        float(min_slope),
        float(min_buy_score),
        before,
        len(out),
        before - len(out),
        int(price_mask.sum()),
        int((buy_s > float(min_buy_score)).sum()),
    )

    return out.reset_index(drop=True)


def _apply_sell_display_guard(df: pd.DataFrame) -> pd.DataFrame:
    """
    SELL TOP10 / AI SELL 表示前フィルタ。

    条件:
      min_price < close <= max_price
      slope < max_sell_slope
      sell_score > 0

    重要:
      slope条件を環境変数で 999 まで緩めても、sell_score=0 の
      BUY銘柄はSELL TOP10に入れない。
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    min_price = _resolve_min_price()
    max_price = _resolve_max_price()
    max_slope = _resolve_max_sell_slope()
    min_sell_score = _resolve_min_sell_score()

    price_s = _select_price_series(out)
    slope_s = _select_slope_series(out)
    sell_s = _select_sell_score_series(out)

    before = len(out)
    price_mask = _apply_price_cap(price_s, min_price, max_price)

    out = out[
        price_mask
        & (slope_s < float(max_slope))
        & (sell_s > float(min_sell_score))
    ].copy()

    logger.info(
        "[SUMMARY DISPLAY SORTING] SELL guard condition='%s and slope < %.4f and sell_score > %.6f' "
        "before=%s after=%s skipped=%s price_ok=%s sell_score_nonzero=%s",
        _price_condition_text(min_price, max_price),
        float(max_slope),
        float(min_sell_score),
        before,
        len(out),
        before - len(out),
        int(price_mask.sum()),
        int((sell_s > float(min_sell_score)).sum()),
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
    """
    BUY TOP10用。

    重要:
      TOP10抽出前に対象外を除外する。
      close <= min_price は除外。
      close > max_price は除外。
      buy_score <= 0 は除外。
    """
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
    """
    SELL TOP10用。

    重要:
      TOP10抽出前に対象外を除外する。
      close <= min_price は除外。
      close > max_price は除外。
      sell_score <= 0 は除外。
    """
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
    """
    AI BUY通過表示用。

    AI通過済みでも、
      close <= min_price
      close > max_price
      buy_score <= 0
    は表示対象外。
    """
    out = prepare_buy_df(df)
    if out.empty:
        return out
    try:
        return out[out["ai_buy_passed_view"].fillna(False)].copy().reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def prepare_ai_sell_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    AI SELL通過表示用。

    AI通過済みでも、
      close <= min_price
      close > max_price
      sell_score <= 0
    は表示対象外。
    """
    out = prepare_sell_df(df)
    if out.empty:
        return out
    try:
        return out[out["ai_sell_passed_view"].fillna(False)].copy().reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def prepare_ai_exit_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    AI EXIT は保有銘柄の決済判定なので、
    価格・slope・buy/sellスコアフィルタはかけない。
    """
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
