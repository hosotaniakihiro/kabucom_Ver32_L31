# ============================================================
# File   : scheduler_jobs/summary/display_runner.py
# Version: V4.3-DISPLAY-UNIVERSE-GUARD
# ------------------------------------------------------------
# ✔ 1分足はDiscord通知しない
# ✔ 3分/5分は通常通り送信
# ✔ 表示直前に close <= 200 を除外
# ✔ BUY対象は slope > 0.03 のみ
# ✔ SELL対象は slope < -0.03 のみ
# ✔ PUSH / RANKING 両方に適用
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Optional, Any

import numpy as np
import pandas as pd

from .dependencies import resolve_display_functions
from .display_prepare import (
    prepare_display_df,
    latest_dt_str,
    symbols_count,
    extract_latest_timestamp,
)
from .quality_guards import (
    looks_uncomputed_push_df,
    looks_uncomputed_ranking_df,
)
from .time_utils import (
    is_fresh_timestamp,
    age_minutes,
    is_lunch_break,
    resolve_display_slot,
    is_market_session,
)

logger = logging.getLogger(__name__)


# ============================================================
# display universe settings
# ============================================================

DEFAULT_DISPLAY_MIN_PRICE = 200.0
DEFAULT_DISPLAY_MIN_BUY_SLOPE = 0.03
DEFAULT_DISPLAY_MAX_SELL_SLOPE = -0.03


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _resolve_display_min_price() -> float:
    """
    表示対象の最低株価。

    200円以下を対象外にするため、判定は price > 200。
    """
    v1 = os.getenv("SUMMARY_DISPLAY_MIN_PRICE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MIN_PRICE", DEFAULT_DISPLAY_MIN_PRICE)

    v2 = os.getenv("TRADE_UNIVERSE_MIN_PRICE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("TRADE_UNIVERSE_MIN_PRICE", DEFAULT_DISPLAY_MIN_PRICE)

    return float(DEFAULT_DISPLAY_MIN_PRICE)


def _resolve_display_min_buy_slope() -> float:
    """
    BUY表示対象の最低slope。

    slope 0.03以下を対象外にするため、判定は slope > 0.03。
    """
    v1 = os.getenv("SUMMARY_DISPLAY_MIN_BUY_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MIN_BUY_SLOPE", DEFAULT_DISPLAY_MIN_BUY_SLOPE)

    v2 = os.getenv("ENTRY_MIN_BUY_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("ENTRY_MIN_BUY_SLOPE", DEFAULT_DISPLAY_MIN_BUY_SLOPE)

    return float(DEFAULT_DISPLAY_MIN_BUY_SLOPE)


def _resolve_display_max_sell_slope() -> float:
    """
    SELL表示対象の最大slope。

    -0.03以上を対象外にするため、判定は slope < -0.03。
    """
    v1 = os.getenv("SUMMARY_DISPLAY_MAX_SELL_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MAX_SELL_SLOPE", DEFAULT_DISPLAY_MAX_SELL_SLOPE)

    v2 = os.getenv("ENTRY_MAX_SELL_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("ENTRY_MAX_SELL_SLOPE", DEFAULT_DISPLAY_MAX_SELL_SLOPE)

    return float(DEFAULT_DISPLAY_MAX_SELL_SLOPE)


def _select_price_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    for c in (
        "disp_close",
        "close",
        "close_price",
        "current_price",
        "price",
        "last_price",
    ):
        if c in df.columns:
            return c

    return None


def _select_slope_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    for c in (
        "disp_slope",
        "slope",
        "score_slope",
        "slope_atr_scaled",
        "ma75_slope",
    ):
        if c in df.columns:
            return c

    return None


def _select_buy_score_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    for c in (
        "disp_buy_score",
        "score_buy",
        "buy_score",
        "buy",
    ):
        if c in df.columns:
            return c

    return None


def _select_sell_score_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    for c in (
        "disp_sell_score",
        "score_sell",
        "sell_score",
        "sell",
    ):
        if c in df.columns:
            return c

    return None


def _apply_display_universe_guard(
    df: pd.DataFrame,
    *,
    interval: int,
    source: str,
) -> pd.DataFrame:
    """
    表示直前の最終防御フィルタ。

    ここで表示用DataFrameそのものから対象外を除去する。

    共通:
      close > 200

    BUY候補として残す条件:
      slope > 0.03

    SELL候補として残す条件:
      slope < -0.03

    注意:
      この関数は表示用の元dfを完全に削る。
      BUYにもSELLにも該当しない中途半端な行は表示対象から外す。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    price_col = _select_price_col(out)
    slope_col = _select_slope_col(out)
    buy_col = _select_buy_score_col(out)
    sell_col = _select_sell_score_col(out)

    min_price = _resolve_display_min_price()
    min_buy_slope = _resolve_display_min_buy_slope()
    max_sell_slope = _resolve_display_max_sell_slope()

    before = len(out)

    if price_col is None:
        logger.warning(
            "[DISPLAY UNIVERSE GUARD] price column missing source=%s interval=%s rows=%s cols=%s",
            source,
            interval,
            before,
            list(out.columns),
        )
        price_s = pd.Series(np.nan, index=out.index)
    else:
        price_s = pd.to_numeric(out[price_col], errors="coerce").fillna(0.0)

    if slope_col is None:
        logger.warning(
            "[DISPLAY UNIVERSE GUARD] slope column missing source=%s interval=%s rows=%s cols=%s",
            source,
            interval,
            before,
            list(out.columns),
        )
        slope_s = pd.Series(0.0, index=out.index)
    else:
        slope_s = pd.to_numeric(out[slope_col], errors="coerce").fillna(0.0)

    if buy_col is None:
        buy_s = pd.Series(0.0, index=out.index)
    else:
        buy_s = pd.to_numeric(out[buy_col], errors="coerce").fillna(0.0)

    if sell_col is None:
        sell_s = pd.Series(0.0, index=out.index)
    else:
        sell_s = pd.to_numeric(out[sell_col], errors="coerce").fillna(0.0).abs()

    price_ok = price_s > float(min_price)

    buy_ok = (
        price_ok
        & (buy_s > 0.0)
        & (slope_s > float(min_buy_slope))
    )

    sell_ok = (
        price_ok
        & (sell_s > 0.0)
        & (slope_s < float(max_sell_slope))
    )

    # BUYにもSELLにも該当しない行は表示対象外
    keep_mask = buy_ok | sell_ok

    out = out.loc[keep_mask].copy()

    after = len(out)

    try:
        skipped_head = []
        if "symbol" in df.columns:
            skipped = df.loc[~keep_mask].copy()
            cols = ["symbol"]
            if price_col:
                cols.append(price_col)
            if slope_col:
                cols.append(slope_col)
            if buy_col:
                cols.append(buy_col)
            if sell_col:
                cols.append(sell_col)
            skipped_head = skipped[cols].head(20).to_dict(orient="records")
    except Exception:
        skipped_head = []

    logger.info(
        "[DISPLAY UNIVERSE GUARD] source=%s interval=%s "
        "price_col=%s slope_col=%s buy_col=%s sell_col=%s "
        "condition='price > %.1f and ((buy > 0 and slope > %.4f) or (sell > 0 and slope < %.4f))' "
        "before=%s after=%s skipped=%s skipped_head=%s",
        source,
        interval,
        price_col,
        slope_col,
        buy_col,
        sell_col,
        float(min_price),
        float(min_buy_slope),
        float(max_sell_slope),
        before,
        after,
        before - after,
        skipped_head,
    )

    return out.reset_index(drop=True)


# ============================================================
# ★ 1分足Discord制御
# ============================================================

def _should_notify_discord(interval: int) -> bool:
    try:
        return int(interval) != 1
    except Exception:
        return True


# ============================================================
# helpers
# ============================================================

def _safe_len(df: Any) -> int:
    try:
        return len(df)
    except Exception:
        return 0


def _safe_cols(df: Any) -> list[str]:
    try:
        if isinstance(df, pd.DataFrame):
            return list(df.columns)
    except Exception:
        pass
    return []


def _safe_df(df: Any) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            return df.copy()
        return pd.DataFrame()
    except Exception:
        logger.debug("[summary.display_runner] safe_df failed", exc_info=True)
        return pd.DataFrame()


# ============================================================
# PUSH表示
# ============================================================

def display_push_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    try:
        display_push, _ = resolve_display_functions()

        if not callable(display_push):
            return

        if not isinstance(df, pd.DataFrame) or df.empty:
            return

        df_prepared = prepare_display_df(df, interval=interval, now=now)
        df_disp = df_prepared if not df_prepared.empty else df

        if df_disp.empty:
            return

        # ----------------------------------------------------
        # 表示直前の最終フィルタ
        # ----------------------------------------------------
        df_disp = _apply_display_universe_guard(
            df_disp,
            interval=interval,
            source="PUSH",
        )

        if df_disp.empty:
            logger.info(
                "[summary.display_runner] PUSH display skipped after universe guard interval=%s",
                interval,
            )
            return

        notify_discord = _should_notify_discord(interval)

        if not notify_discord:
            logger.info("[DISCORD] skip 1min PUSH summary")

        try:
            display_push(
                summary_df=df_disp,
                interval=interval,
                interval_label=f"{interval}min",
                now=now,
                notify_discord=notify_discord,
            )
        except TypeError:
            display_push(
                summary_df=df_disp,
                interval=interval,
                interval_label=f"{interval}min",
                now=now,
            )

    except Exception:
        logger.exception("[display_runner] push display failed")


# ============================================================
# RANKING表示
# ============================================================

def display_ranking_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    try:
        _, display_ranking = resolve_display_functions()

        if not callable(display_ranking):
            return

        if not isinstance(df, pd.DataFrame) or df.empty:
            return

        df_prepared = prepare_display_df(df, interval=interval, now=now)
        df_disp = df_prepared if not df_prepared.empty else df

        if df_disp.empty:
            return

        # ----------------------------------------------------
        # 表示直前の最終フィルタ
        # ----------------------------------------------------
        df_disp = _apply_display_universe_guard(
            df_disp,
            interval=interval,
            source="RANKING",
        )

        if df_disp.empty:
            logger.info(
                "[summary.display_runner] RANKING display skipped after universe guard interval=%s",
                interval,
            )
            return

        notify_discord = _should_notify_discord(interval)

        if not notify_discord:
            logger.info("[DISCORD] skip 1min RANKING summary")

        try:
            display_ranking(
                summary_df=df_disp,
                interval=interval,
                interval_label=f"{interval}min",
                now=now,
                notify_discord=notify_discord,
            )
        except TypeError:
            display_ranking(
                summary_df=df_disp,
                interval=interval,
                interval_label=f"{interval}min",
                now=now,
            )

    except Exception:
        logger.exception("[display_runner] ranking display failed")


# ============================================================
# CLOSED DAY
# ============================================================

def display_closed_day_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    display_push_summary(df=df, interval=interval, now=now)


# ============================================================
# aliases
# ============================================================

def run_display_push_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    display_push_summary(df=df, interval=interval, now=now)


def run_display_ranking_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    display_ranking_summary(df=df, interval=interval, now=now)


__all__ = [
    "display_push_summary",
    "display_ranking_summary",
    "display_closed_day_summary",
    "run_display_push_summary",
    "run_display_ranking_summary",
]