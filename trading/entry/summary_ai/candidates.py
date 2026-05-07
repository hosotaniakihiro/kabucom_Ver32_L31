# ============================================================
# File   : trading/entry/summary_ai/candidates.py
# Version: PRODUCTION-STABLE-REV2.0-BUY-SELL-TOP20-CANDIDATES
# ------------------------------------------------------------
# Purpose:
#   - SUMMARY / RANKING SUMMARY の DataFrame からAI gate候補を作る
#   - build_summary_ai_entry_candidates() で BUY TOP20 と SELL TOP20 を同時に返す
#   - 各行に ai_side / side = BUY or SELL を付与し、AI gate側で行ごとに判定する
#
# Important:
#   - 既存runnerが build_summary_ai_entry_candidates() だけを呼んでも、
#     BUY候補とSELL候補の両方がAIへ渡る
#   - BUY候補: close > 200, volume条件, slope > 閾値, buy score優勢
#   - SELL候補: close > 200, volume条件, slope < 閾値, sell score優勢
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Optional

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

DEFAULT_TOP_N = 20
DEFAULT_MIN_BUY_SCORE = 5.0
DEFAULT_MAX_SELL_SCORE = 2.0
DEFAULT_MIN_VOLUME = 1.0
DEFAULT_MIN_PRICE = 200.0
DEFAULT_MIN_BUY_SLOPE = 0.01
DEFAULT_MAX_SELL_SLOPE = -0.01


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "y"}:
            return True
        if s in {"0", "false", "no", "off", "n", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _entry_min_price(default: float = DEFAULT_MIN_PRICE) -> float:
    for name in ("ENTRY_MIN_PRICE", "SUMMARY_AI_ENTRY_MIN_PRICE"):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, default)
    return float(default)


def _entry_min_buy_slope() -> float:
    for name in ("ENTRY_MIN_BUY_SLOPE", "SUMMARY_AI_MIN_BUY_SLOPE"):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, DEFAULT_MIN_BUY_SLOPE)
    return float(DEFAULT_MIN_BUY_SLOPE)


def _entry_max_sell_slope() -> float:
    for name in ("ENTRY_MAX_SELL_SLOPE", "SUMMARY_AI_MAX_SELL_SLOPE"):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, DEFAULT_MAX_SELL_SLOPE)
    return float(DEFAULT_MAX_SELL_SLOPE)


def _safe_symbols(df: pd.DataFrame, n: int = 30) -> list[str]:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            return list(df["symbol"].astype(str).head(n))
    except Exception:
        pass
    return []


def attach_display_like_columns(df: pd.DataFrame) -> pd.DataFrame:
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

    out["ai_disp_buy_score"] = pick_num_series(out, ["disp_buy_score", "score_buy", "buy_score", "buy"], 0.0)
    out["ai_disp_sell_score"] = pick_num_series(out, ["disp_sell_score", "score_sell", "sell_score", "sell"], 0.0).abs()
    out["ai_disp_score"] = pick_num_series(out, ["disp_score", "display_score", "score", "final_score"], 0.0)
    out["ai_disp_total_score"] = pick_num_series(out, ["disp_total_score", "score_total", "total_score", "combined_score", "final_score", "display_score", "score"], 0.0)
    if float(out["ai_disp_total_score"].abs().sum()) == 0.0:
        out["ai_disp_total_score"] = out["ai_disp_buy_score"] - out["ai_disp_sell_score"]
    out["ai_disp_final_score"] = pick_num_series(out, ["disp_final_score", "final_score", "display_score", "score_total", "score"], 0.0)
    out["ai_disp_close"] = pick_num_series(out, ["disp_close", "close", "close_price", "current_price", "price", "last_price"], 0.0)
    out["ai_disp_volume"] = pick_num_series(out, ["volume", "trading_volume", "出来高"], 0.0)
    out["ai_disp_turnover"] = pick_num_series(out, ["turnover", "trading_value", "売買代金", "ai_turnover"], 0.0)
    if float(out["ai_disp_turnover"].abs().sum()) == 0.0:
        out["ai_disp_turnover"] = out["ai_disp_close"] * out["ai_disp_volume"]
    out["ai_disp_slope"] = pick_num_series(out, ["disp_slope", "slope", "slope_atr_scaled", "score_slope"], 0.0)
    out["ai_disp_mtf"] = pick_num_series(out, ["disp_mtf", "score_mtf", "mtf_score", "mtf"], 0.0)
    out["ai_disp_rsi"] = pick_num_series(out, ["disp_rsi", "rsi", "RSI"], 50.0)
    out["ai_disp_macd"] = pick_num_series(out, ["disp_macd", "macd", "MACD"], 0.0)
    out["ai_disp_signal"] = pick_num_series(out, ["disp_signal", "signal", "macd_signal", "SIGNAL"], 0.0)
    out["ai_score_base"] = pick_num_series(out, ["disp_base", "score_base", "breakdown_base", "base"], 0.0)
    out["ai_score_trend"] = pick_num_series(out, ["disp_trend", "score_trend", "breakdown_trend", "trend"], 0.0)
    out["ai_score_momentum"] = pick_num_series(out, ["disp_mom", "score_momentum", "breakdown_mom", "mom", "momentum"], 0.0)
    out["ai_score_velocity"] = pick_num_series(out, ["disp_vel", "score_velocity", "breakdown_vel", "vel", "velocity"], 0.0)
    out["ai_score_penalty"] = pick_num_series(out, ["disp_pen", "score_penalty", "breakdown_pen", "pen", "penalty"], 0.0)

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


def filter_common_stock_rows(df: pd.DataFrame, *, require_buy_target: bool = False, exclude_etf_fund: bool = True, allowed_market_types: Optional[set[str]] = None) -> pd.DataFrame:
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

            name_col = next((c for c in ("symbolname_view", "symbolname", "name") if c in out.columns), None)
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
            out = out[mt.isin(allowed_market_types) | mt.eq("")].copy()

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
            ("ai_disp_sell_score", 10),
            ("ai_disp_total_score", 8),
            ("ai_disp_final_score", 8),
            ("ai_disp_close", 4),
            ("ai_disp_volume", 3),
            ("ai_disp_rsi", 2),
            ("ai_disp_macd", 2),
        ]:
            if col in out.columns:
                quality += pd.to_numeric(out[col], errors="coerce").notna().astype(int) * weight
        out["_ai_quality"] = quality
        sort_cols = ["symbol", "_ai_quality"]
        ascending = [True, False]
        if "datetime" in out.columns:
            sort_cols.append("datetime")
            ascending.append(False)
        out = out.sort_values(sort_cols, ascending=ascending, na_position="last", kind="mergesort")
        out = out.drop_duplicates(subset=["symbol"], keep="first")
        return out.drop(columns=["_ai_quality"], errors="ignore").reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY AI CANDIDATES] dedupe failed")
        return out.reset_index(drop=True)


def _prepare_base(summary_df: pd.DataFrame, *, require_buy_target: bool, exclude_etf_fund: bool) -> pd.DataFrame:
    df = attach_display_like_columns(summary_df)
    if df.empty:
        return df
    df = filter_common_stock_rows(df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if df.empty:
        return df
    return dedupe_one_row_per_symbol(df)


def _buy_candidates_from_prepared(df: pd.DataFrame, *, interval: int | str, top_n: int, min_buy_score: float, max_sell_score: float, min_volume: float, min_price: float, source: str) -> pd.DataFrame:
    if df.empty:
        return df
    resolved_min_price = max(float(min_price), _entry_min_price(DEFAULT_MIN_PRICE))
    resolved_min_buy_slope = _entry_min_buy_slope()
    before = len(df)
    out = df[
        (pd.to_numeric(df["ai_disp_buy_score"], errors="coerce").fillna(0.0) >= float(min_buy_score))
        & (pd.to_numeric(df["ai_disp_sell_score"], errors="coerce").fillna(0.0) <= float(max_sell_score))
        & (pd.to_numeric(df["ai_disp_close"], errors="coerce").fillna(0.0) > float(resolved_min_price))
        & (pd.to_numeric(df["ai_disp_volume"], errors="coerce").fillna(0.0) >= float(min_volume))
        & (pd.to_numeric(df["ai_disp_slope"], errors="coerce").fillna(0.0) > float(resolved_min_buy_slope))
    ].copy()
    if out.empty:
        logger.warning("[SUMMARY AI CANDIDATES] BUY empty interval=%s source=%s before=%s", interval, source, before)
        return out
    out["_ai_sort_score"] = (
        pd.to_numeric(out["ai_disp_buy_score"], errors="coerce").fillna(0.0) * 10.0
        + pd.to_numeric(out["ai_disp_total_score"], errors="coerce").fillna(0.0) * 3.0
        + pd.to_numeric(out["ai_disp_mtf"], errors="coerce").fillna(0.0)
        + pd.to_numeric(out["ai_disp_slope"], errors="coerce").fillna(0.0)
        - pd.to_numeric(out["ai_disp_sell_score"], errors="coerce").fillna(0.0) * 5.0
    )
    out = out.sort_values(["_ai_sort_score", "ai_disp_buy_score", "ai_disp_total_score"], ascending=[False, False, False], na_position="last", kind="mergesort").head(int(top_n))
    out = out.drop(columns=["_ai_sort_score"], errors="ignore").reset_index(drop=True)
    out["ai_side"] = "BUY"
    out["side"] = "BUY"
    out["entry_decision"] = "BUY"
    logger.warning("[SUMMARY AI CANDIDATES] BUY_TOP_READY interval=%s source=%s count=%s top_n=%s symbols=%s", interval, source, len(out), top_n, _safe_symbols(out, int(top_n)))
    return out


def _sell_candidates_from_prepared(df: pd.DataFrame, *, interval: int | str, top_n: int, min_sell_score: float, max_buy_score: float, min_volume: float, min_price: float, source: str) -> pd.DataFrame:
    if df.empty:
        return df
    resolved_min_price = max(float(min_price), _entry_min_price(DEFAULT_MIN_PRICE))
    resolved_max_sell_slope = _entry_max_sell_slope()
    before = len(df)
    out = df[
        (pd.to_numeric(df["ai_disp_sell_score"], errors="coerce").fillna(0.0) >= float(min_sell_score))
        & (pd.to_numeric(df["ai_disp_buy_score"], errors="coerce").fillna(0.0) <= float(max_buy_score))
        & (pd.to_numeric(df["ai_disp_close"], errors="coerce").fillna(0.0) > float(resolved_min_price))
        & (pd.to_numeric(df["ai_disp_volume"], errors="coerce").fillna(0.0) >= float(min_volume))
        & (pd.to_numeric(df["ai_disp_slope"], errors="coerce").fillna(0.0) < float(resolved_max_sell_slope))
    ].copy()
    if out.empty:
        logger.warning("[SUMMARY AI CANDIDATES] SELL empty interval=%s source=%s before=%s", interval, source, before)
        return out
    out["_ai_sort_score"] = (
        pd.to_numeric(out["ai_disp_sell_score"], errors="coerce").fillna(0.0) * 10.0
        - pd.to_numeric(out["ai_disp_buy_score"], errors="coerce").fillna(0.0) * 5.0
        - pd.to_numeric(out["ai_disp_slope"], errors="coerce").fillna(0.0) * 3.0
        - pd.to_numeric(out["ai_disp_total_score"], errors="coerce").fillna(0.0)
    )
    out = out.sort_values(["_ai_sort_score", "ai_disp_sell_score"], ascending=[False, False], na_position="last", kind="mergesort").head(int(top_n))
    out = out.drop(columns=["_ai_sort_score"], errors="ignore").reset_index(drop=True)
    out["ai_side"] = "SELL"
    out["side"] = "SELL"
    out["entry_decision"] = "SELL"
    logger.warning("[SUMMARY AI CANDIDATES] SELL_TOP_READY interval=%s source=%s count=%s top_n=%s symbols=%s", interval, source, len(out), top_n, _safe_symbols(out, int(top_n)))
    return out


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
    既存runner互換の候補作成入口。

    重要:
      既存runnerはこの関数しか呼ばないため、ここでBUY TOP20とSELL TOP20を結合して返す。
      ai_gate_runner.py は行ごとの ai_side / side を読んで BUY/SELL としてAIに渡す。
    """
    try:
        top_n = max(1, int(top_n or DEFAULT_TOP_N))
    except Exception:
        top_n = DEFAULT_TOP_N

    base = _prepare_base(summary_df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if base.empty:
        logger.info("[SUMMARY AI CANDIDATES] no rows after base prepare interval=%s source=%s", interval, source)
        return base

    buy_df = _buy_candidates_from_prepared(
        base,
        interval=interval,
        top_n=top_n,
        min_buy_score=min_buy_score,
        max_sell_score=max_sell_score,
        min_volume=min_volume,
        min_price=min_price,
        source=source,
    )
    sell_df = _sell_candidates_from_prepared(
        base,
        interval=interval,
        top_n=top_n,
        min_sell_score=0.01,
        max_buy_score=999999.0,
        min_volume=min_volume,
        min_price=min_price,
        source=source,
    )

    frames = [x for x in (buy_df, sell_df) if isinstance(x, pd.DataFrame) and not x.empty]
    if not frames:
        logger.warning("[SUMMARY AI CANDIDATES] BUY_SELL combined empty interval=%s source=%s base_rows=%s", interval, source, len(base))
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True, sort=False)
    logger.warning(
        "[SUMMARY AI CANDIDATES] BUY_SELL_COMBINED_READY interval=%s source=%s buy_count=%s sell_count=%s total=%s top_n_each=%s buy_symbols=%s sell_symbols=%s",
        interval,
        source,
        len(buy_df) if isinstance(buy_df, pd.DataFrame) else 0,
        len(sell_df) if isinstance(sell_df, pd.DataFrame) else 0,
        len(out),
        top_n,
        _safe_symbols(buy_df, top_n),
        _safe_symbols(sell_df, top_n),
    )
    return out.reset_index(drop=True)


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
    base = _prepare_base(summary_df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if base.empty:
        return base
    return _sell_candidates_from_prepared(
        base,
        interval=interval,
        top_n=int(top_n or DEFAULT_TOP_N),
        min_sell_score=min_sell_score,
        max_buy_score=max_buy_score,
        min_volume=min_volume,
        min_price=min_price,
        source=source,
    )


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
