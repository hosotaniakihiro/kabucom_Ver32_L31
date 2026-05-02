# ============================================================
# File   : trading/entry/summary_ai/candidates.py
# Version: PRODUCTION-STABLE-REV1.1-SUMMARY-AI-CANDIDATES-ENTRY-GUARD
# ------------------------------------------------------------
# 【概要】
#   SUMMARY / RANKING SUMMARY の DataFrame から、
#   AI gate に確認するエントリー候補を作成する。
#
# 【主な機能】
#   - display.py を通っていないDFでも ai_disp_* を補完
#   - ETF / FUND / REIT 除外
#   - buy_target 任意チェック
#   - symbol ごと1行へ重複除去
#   - buy score / sell score / volume / price filter
#   - TOP N 抽出
#
# 【REV1.1 修正】
#   - エントリー候補の最低株価を 200円超に変更
#   - BUY候補は slope > 0.03 のみ通過
#   - SELL候補は slope < -0.03 のみ通過
#   - close <= 200 は AI gate / entry候補から除外
#
# 【重要条件】
#   BUY:
#       close > 200
#       slope > 0.03
#
#   SELL:
#       close > 200
#       slope < -0.03
#
#   つまり、
#       close = 200.0 は対象外
#       BUY slope = 0.03 は対象外
#       SELL slope = -0.03 は対象外
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import pandas as pd

from .utils import (
    VALID_MARKET_TYPES,
    is_truthy,
    normalize_symbol,
    pick_num_series,
    pick_text_series,
    safe_df,
)

logger = logging.getLogger(__name__)


DEFAULT_TOP_N = 10
DEFAULT_MIN_BUY_SCORE = 5.0
DEFAULT_MAX_SELL_SCORE = 2.0
DEFAULT_MIN_VOLUME = 1.0

# 200円以下はエントリー候補から除外
DEFAULT_MIN_PRICE = 200.0

# BUYは 0.03 以下を除外
DEFAULT_MIN_BUY_SLOPE = 0.01

# SELLは -0.03 以上を除外
DEFAULT_MAX_SELL_SLOPE = -0.01


# ============================================================
# env helpers
# ============================================================

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _entry_min_price(default: float = DEFAULT_MIN_PRICE) -> float:
    """
    エントリー候補の最低株価。

    優先順位:
      1. ENTRY_MIN_PRICE
      2. SUMMARY_AI_ENTRY_MIN_PRICE
      3. default

    判定は close > min_price。
    """
    v1 = os.getenv("ENTRY_MIN_PRICE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("ENTRY_MIN_PRICE", default)

    v2 = os.getenv("SUMMARY_AI_ENTRY_MIN_PRICE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("SUMMARY_AI_ENTRY_MIN_PRICE", default)

    return float(default)


def _entry_min_buy_slope() -> float:
    """
    BUY候補の最低slope。

    判定は slope > min_buy_slope。
    """
    v1 = os.getenv("ENTRY_MIN_BUY_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("ENTRY_MIN_BUY_SLOPE", DEFAULT_MIN_BUY_SLOPE)

    v2 = os.getenv("SUMMARY_AI_MIN_BUY_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("SUMMARY_AI_MIN_BUY_SLOPE", DEFAULT_MIN_BUY_SLOPE)

    return float(DEFAULT_MIN_BUY_SLOPE)


def _entry_max_sell_slope() -> float:
    """
    SELL候補の最大slope。

    判定は slope < max_sell_slope。
    max_sell_slope=-0.03 の場合、
      slope=-0.03 は対象外
      slope=-0.031 は通過
    """
    v1 = os.getenv("ENTRY_MAX_SELL_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("ENTRY_MAX_SELL_SLOPE", DEFAULT_MAX_SELL_SLOPE)

    v2 = os.getenv("SUMMARY_AI_MAX_SELL_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("SUMMARY_AI_MAX_SELL_SLOPE", DEFAULT_MAX_SELL_SLOPE)

    return float(DEFAULT_MAX_SELL_SLOPE)


# ============================================================
# display-like column attach
# ============================================================

def attach_display_like_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    display.py を通っていない DataFrame でも TOP10 抽出できるように、
    ai_disp_* 系の列を補完する。
    """
    out = safe_df(df)
    if out.empty:
        return out

    if "symbol" not in out.columns:
        logger.warning("[SUMMARY AI CANDIDATES] missing symbol column")
        return pd.DataFrame()

    out["symbol"] = out["symbol"].map(normalize_symbol)
    out = out[out["symbol"].astype(str).str.strip() != ""].copy()
    if out.empty:
        return out

    if "symbolname_view" not in out.columns:
        symbolname = pick_text_series(out, ["symbolname", "name", "display_name"], "")
        out["symbolname_view"] = symbolname.mask(symbolname.str.strip().eq(""), out["symbol"])

    out["ai_disp_buy_score"] = pick_num_series(
        out,
        ["disp_buy_score", "score_buy", "buy_score", "buy"],
        0.0,
    )

    out["ai_disp_sell_score"] = pick_num_series(
        out,
        ["disp_sell_score", "score_sell", "sell_score", "sell"],
        0.0,
    ).abs()

    out["ai_disp_score"] = pick_num_series(
        out,
        ["disp_score", "display_score", "score", "final_score"],
        0.0,
    )

    out["ai_disp_total_score"] = pick_num_series(
        out,
        [
            "disp_total_score",
            "score_total",
            "total_score",
            "combined_score",
            "final_score",
            "display_score",
            "score",
        ],
        0.0,
    )

    if float(out["ai_disp_total_score"].abs().sum()) == 0.0:
        out["ai_disp_total_score"] = out["ai_disp_buy_score"] - out["ai_disp_sell_score"]

    out["ai_disp_final_score"] = pick_num_series(
        out,
        ["disp_final_score", "final_score", "display_score", "score_total", "score"],
        0.0,
    )

    out["ai_disp_close"] = pick_num_series(
        out,
        ["disp_close", "close", "close_price", "current_price", "price", "last_price"],
        0.0,
    )

    out["ai_disp_volume"] = pick_num_series(
        out,
        ["volume", "trading_volume", "出来高"],
        0.0,
    )

    out["ai_disp_turnover"] = pick_num_series(
        out,
        ["turnover", "trading_value", "売買代金", "ai_turnover"],
        0.0,
    )

    if float(out["ai_disp_turnover"].abs().sum()) == 0.0:
        out["ai_disp_turnover"] = out["ai_disp_close"] * out["ai_disp_volume"]

    out["ai_disp_slope"] = pick_num_series(
        out,
        ["disp_slope", "slope", "slope_atr_scaled", "score_slope"],
        0.0,
    )

    out["ai_disp_mtf"] = pick_num_series(
        out,
        ["disp_mtf", "score_mtf", "mtf_score", "mtf"],
        0.0,
    )

    out["ai_disp_rsi"] = pick_num_series(
        out,
        ["disp_rsi", "rsi", "RSI"],
        50.0,
    )

    out["ai_disp_macd"] = pick_num_series(
        out,
        ["disp_macd", "macd", "MACD"],
        0.0,
    )

    out["ai_disp_signal"] = pick_num_series(
        out,
        ["disp_signal", "signal", "macd_signal", "SIGNAL"],
        0.0,
    )

    out["ai_score_base"] = pick_num_series(
        out,
        ["disp_base", "score_base", "breakdown_base", "base"],
        0.0,
    )

    out["ai_score_trend"] = pick_num_series(
        out,
        ["disp_trend", "score_trend", "breakdown_trend", "trend"],
        0.0,
    )

    out["ai_score_momentum"] = pick_num_series(
        out,
        ["disp_mom", "score_momentum", "breakdown_mom", "mom", "momentum"],
        0.0,
    )

    out["ai_score_velocity"] = pick_num_series(
        out,
        ["disp_vel", "score_velocity", "breakdown_vel", "vel", "velocity"],
        0.0,
    )

    out["ai_score_penalty"] = pick_num_series(
        out,
        ["disp_pen", "score_penalty", "breakdown_pen", "pen", "penalty"],
        0.0,
    )

    if "datetime" in out.columns:
        try:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
        except Exception:
            logger.debug("[SUMMARY AI CANDIDATES] datetime normalize failed", exc_info=True)

    return out.reset_index(drop=True)


# ============================================================
# common filters
# ============================================================

def filter_common_stock_rows(
    df: pd.DataFrame,
    *,
    require_buy_target: bool = False,
    exclude_etf_fund: bool = True,
    allowed_market_types: Optional[set[str]] = None,
) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    allowed_market_types = allowed_market_types or VALID_MARKET_TYPES

    try:
        if require_buy_target and "buy_target" in out.columns:
            out = out[out["buy_target"].map(lambda x: is_truthy(x, False))].copy()

        if exclude_etf_fund:
            for col in ("is_etf", "is_reit", "is_fund"):
                if col in out.columns:
                    out = out[~out[col].map(lambda x: is_truthy(x, False))].copy()

            name_col = None
            for c in ("symbolname_view", "symbolname", "name"):
                if c in out.columns:
                    name_col = c
                    break

            if name_col:
                s = out[name_col].fillna("").astype(str).str.upper()
                mask = (
                    s.str.contains("ETF", na=False)
                    | s.str.contains("ETN", na=False)
                    | s.str.contains("REIT", na=False)
                    | s.str.contains("リート", na=False)
                    | s.str.contains("投信", na=False)
                    | s.str.contains("FUND", na=False)
                    | s.str.contains("ＦＵＮＤ", na=False)
                )
                out = out[~mask].copy()

        if "market_type" in out.columns:
            mt = out["market_type"].fillna("").astype(str).str.strip()
            mask = mt.isin(allowed_market_types) | mt.eq("")
            out = out[mask].copy()

    except Exception:
        logger.debug("[SUMMARY AI CANDIDATES] common stock filter failed", exc_info=True)

    return out.reset_index(drop=True)


def dedupe_one_row_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        quality = pd.Series(0, index=out.index, dtype="int64")

        for col, weight in [
            ("ai_disp_buy_score", 10),
            ("ai_disp_total_score", 8),
            ("ai_disp_final_score", 8),
            ("ai_disp_close", 4),
            ("ai_disp_volume", 3),
            ("ai_disp_rsi", 2),
            ("ai_disp_macd", 2),
            ("ai_score_base", 1),
            ("ai_score_trend", 1),
            ("ai_score_momentum", 1),
            ("ai_score_velocity", 1),
            ("ai_score_penalty", 1),
        ]:
            if col not in out.columns:
                continue

            s = pd.to_numeric(out[col], errors="coerce")
            quality += s.notna().astype(int) * weight

        out["_ai_quality"] = quality

        sort_cols = ["symbol", "_ai_quality"]
        ascending = [True, False]

        if "datetime" in out.columns:
            sort_cols.append("datetime")
            ascending.append(False)

        out = out.sort_values(
            sort_cols,
            ascending=ascending,
            na_position="last",
            kind="mergesort",
        )
        out = out.drop_duplicates(subset=["symbol"], keep="first")
        return out.drop(columns=["_ai_quality"], errors="ignore").reset_index(drop=True)

    except Exception:
        logger.exception("[SUMMARY AI CANDIDATES] dedupe failed")
        return out


# ============================================================
# entry candidate guards
# ============================================================

def _apply_buy_entry_guard(
    df: pd.DataFrame,
    *,
    interval: int | str,
    source: str,
    min_price: float,
    min_buy_slope: float,
) -> pd.DataFrame:
    """
    BUY entry候補の最終ガード。

    条件:
      ai_disp_close > min_price
      ai_disp_slope > min_buy_slope
    """
    out = safe_df(df)
    if out.empty:
        return out

    before = len(out)

    close_s = pd.to_numeric(out["ai_disp_close"], errors="coerce").fillna(0.0)
    slope_s = pd.to_numeric(out["ai_disp_slope"], errors="coerce").fillna(0.0)

    out = out[
        (close_s > float(min_price))
        & (slope_s > float(min_buy_slope))
    ].copy()

    logger.info(
        "[SUMMARY AI CANDIDATES] BUY entry guard interval=%s source=%s "
        "condition='close > %.1f and slope > %.4f' before=%s after=%s skipped=%s",
        interval,
        source,
        float(min_price),
        float(min_buy_slope),
        before,
        len(out),
        before - len(out),
    )

    return out.reset_index(drop=True)


def _apply_sell_entry_guard(
    df: pd.DataFrame,
    *,
    interval: int | str,
    source: str,
    min_price: float,
    max_sell_slope: float,
) -> pd.DataFrame:
    """
    SELL entry候補の最終ガード。

    条件:
      ai_disp_close > min_price
      ai_disp_slope < max_sell_slope

    max_sell_slope=-0.03 の場合、
      -0.03 以上は対象外。
    """
    out = safe_df(df)
    if out.empty:
        return out

    before = len(out)

    close_s = pd.to_numeric(out["ai_disp_close"], errors="coerce").fillna(0.0)
    slope_s = pd.to_numeric(out["ai_disp_slope"], errors="coerce").fillna(0.0)

    out = out[
        (close_s > float(min_price))
        & (slope_s < float(max_sell_slope))
    ].copy()

    logger.info(
        "[SUMMARY AI CANDIDATES] SELL entry guard interval=%s source=%s "
        "condition='close > %.1f and slope < %.4f' before=%s after=%s skipped=%s",
        interval,
        source,
        float(min_price),
        float(max_sell_slope),
        before,
        len(out),
        before - len(out),
    )

    return out.reset_index(drop=True)


# ============================================================
# public candidate builders
# ============================================================

def build_summary_ai_entry_candidates(
    summary_df: pd.DataFrame,
    *,
    interval: int | str = 1,
    top_n: int = DEFAULT_TOP_N,
    min_buy_score: float = DEFAULT_MIN_BUY_SCORE,
    max_sell_score: float = DEFAULT_MAX_SELL_SCORE,
    min_volume: float = DEFAULT_MIN_VOLUME,
    min_price: float = DEFAULT_MIN_PRICE,
    require_buy_target: bool = False,
    exclude_etf_fund: bool = True,
    source: str = "SUMMARY",
) -> pd.DataFrame:
    """
    summary_df から AI に確認する BUY TOP candidates を作る。

    BUY候補条件:
      - score_buy >= min_buy_score
      - score_sell <= max_sell_score
      - close > 200
      - volume >= min_volume
      - slope > 0.03
    """
    df = attach_display_like_columns(summary_df)
    if df.empty:
        logger.info("[SUMMARY AI CANDIDATES] no summary rows")
        return df

    df = filter_common_stock_rows(
        df,
        require_buy_target=require_buy_target,
        exclude_etf_fund=exclude_etf_fund,
    )
    if df.empty:
        logger.info("[SUMMARY AI CANDIDATES] no rows after common stock filter")
        return df

    df = dedupe_one_row_per_symbol(df)
    if df.empty:
        return df

    # runner.py から min_price=1.0 が渡ってきても、実質200円超を強制する。
    resolved_min_price = max(float(min_price), _entry_min_price(DEFAULT_MIN_PRICE))
    resolved_min_buy_slope = _entry_min_buy_slope()

    before = len(df)

    df = df[
        (pd.to_numeric(df["ai_disp_buy_score"], errors="coerce").fillna(0.0) >= float(min_buy_score))
        & (pd.to_numeric(df["ai_disp_sell_score"], errors="coerce").fillna(0.0) <= float(max_sell_score))
        & (pd.to_numeric(df["ai_disp_close"], errors="coerce").fillna(0.0) > float(resolved_min_price))
        & (pd.to_numeric(df["ai_disp_volume"], errors="coerce").fillna(0.0) >= float(min_volume))
    ].copy()

    after_basic = len(df)

    df = _apply_buy_entry_guard(
        df,
        interval=interval,
        source=source,
        min_price=resolved_min_price,
        min_buy_slope=resolved_min_buy_slope,
    )

    logger.info(
        "[SUMMARY AI CANDIDATES] BUY candidate filter interval=%s source=%s before=%s after_basic=%s after_guard=%s "
        "min_buy_score=%.2f max_sell_score=%.2f min_volume=%.1f min_price=%.1f min_buy_slope=%.4f",
        interval,
        source,
        before,
        after_basic,
        len(df),
        min_buy_score,
        max_sell_score,
        min_volume,
        resolved_min_price,
        resolved_min_buy_slope,
    )

    if df.empty:
        return df

    df["_ai_sort_score"] = (
        pd.to_numeric(df["ai_disp_buy_score"], errors="coerce").fillna(0.0) * 10.0
        + pd.to_numeric(df["ai_disp_total_score"], errors="coerce").fillna(0.0) * 3.0
        + pd.to_numeric(df["ai_disp_mtf"], errors="coerce").fillna(0.0)
        + pd.to_numeric(df["ai_disp_slope"], errors="coerce").fillna(0.0)
        - pd.to_numeric(df["ai_disp_sell_score"], errors="coerce").fillna(0.0) * 5.0
    )

    df = df.sort_values(
        by=["_ai_sort_score", "ai_disp_buy_score", "ai_disp_total_score"],
        ascending=[False, False, False],
        na_position="last",
        kind="mergesort",
    ).head(int(top_n))

    return df.drop(columns=["_ai_sort_score"], errors="ignore").reset_index(drop=True)


def build_summary_ai_sell_entry_candidates(
    summary_df: pd.DataFrame,
    *,
    interval: int | str = 1,
    top_n: int = DEFAULT_TOP_N,
    min_sell_score: float = 0.01,
    max_buy_score: float = 2.0,
    min_volume: float = DEFAULT_MIN_VOLUME,
    min_price: float = DEFAULT_MIN_PRICE,
    require_buy_target: bool = False,
    exclude_etf_fund: bool = True,
    source: str = "SUMMARY",
) -> pd.DataFrame:
    """
    summary_df から AI に確認する SELL candidates を作る。

    SELL候補条件:
      - score_sell >= min_sell_score
      - score_buy <= max_buy_score
      - close > 200
      - volume >= min_volume
      - slope < -0.03

    既存側がこの関数を呼ばない場合でも、
    将来 SELL AI entry を使う時のために用意しておく。
    """
    df = attach_display_like_columns(summary_df)
    if df.empty:
        logger.info("[SUMMARY AI SELL CANDIDATES] no summary rows")
        return df

    df = filter_common_stock_rows(
        df,
        require_buy_target=require_buy_target,
        exclude_etf_fund=exclude_etf_fund,
    )
    if df.empty:
        logger.info("[SUMMARY AI SELL CANDIDATES] no rows after common stock filter")
        return df

    df = dedupe_one_row_per_symbol(df)
    if df.empty:
        return df

    resolved_min_price = max(float(min_price), _entry_min_price(DEFAULT_MIN_PRICE))
    resolved_max_sell_slope = _entry_max_sell_slope()

    before = len(df)

    df = df[
        (pd.to_numeric(df["ai_disp_sell_score"], errors="coerce").fillna(0.0) >= float(min_sell_score))
        & (pd.to_numeric(df["ai_disp_buy_score"], errors="coerce").fillna(0.0) <= float(max_buy_score))
        & (pd.to_numeric(df["ai_disp_close"], errors="coerce").fillna(0.0) > float(resolved_min_price))
        & (pd.to_numeric(df["ai_disp_volume"], errors="coerce").fillna(0.0) >= float(min_volume))
    ].copy()

    after_basic = len(df)

    df = _apply_sell_entry_guard(
        df,
        interval=interval,
        source=source,
        min_price=resolved_min_price,
        max_sell_slope=resolved_max_sell_slope,
    )

    logger.info(
        "[SUMMARY AI CANDIDATES] SELL candidate filter interval=%s source=%s before=%s after_basic=%s after_guard=%s "
        "min_sell_score=%.2f max_buy_score=%.2f min_volume=%.1f min_price=%.1f max_sell_slope=%.4f",
        interval,
        source,
        before,
        after_basic,
        len(df),
        min_sell_score,
        max_buy_score,
        min_volume,
        resolved_min_price,
        resolved_max_sell_slope,
    )

    if df.empty:
        return df

    df["_ai_sort_score"] = (
        pd.to_numeric(df["ai_disp_sell_score"], errors="coerce").fillna(0.0) * 10.0
        - pd.to_numeric(df["ai_disp_buy_score"], errors="coerce").fillna(0.0) * 5.0
        - pd.to_numeric(df["ai_disp_slope"], errors="coerce").fillna(0.0) * 3.0
        - pd.to_numeric(df["ai_disp_total_score"], errors="coerce").fillna(0.0)
    )

    df = df.sort_values(
        by=["_ai_sort_score", "ai_disp_sell_score"],
        ascending=[False, False],
        na_position="last",
        kind="mergesort",
    ).head(int(top_n))

    return df.drop(columns=["_ai_sort_score"], errors="ignore").reset_index(drop=True)


__all__ = [
    "DEFAULT_TOP_N",
    "DEFAULT_MIN_BUY_SCORE",
    "DEFAULT_MAX_SELL_SCORE",
    "DEFAULT_MIN_VOLUME",
    "DEFAULT_MIN_PRICE",
    "DEFAULT_MIN_BUY_SLOPE",
    "DEFAULT_MAX_SELL_SLOPE",
    "attach_display_like_columns",
    "filter_common_stock_rows",
    "dedupe_one_row_per_symbol",
    "build_summary_ai_entry_candidates",
    "build_summary_ai_sell_entry_candidates",
]