# ============================================================
# File   : trading/summary/recovery/rebuilders.py
# Ver    : PRODUCTION-STABLE-REV7.0-DELTA-FIRST-REBUILDERS
# ------------------------------------------------------------
# 【概要】
#   サマリー再構築ロジック集
#
# 【主な機能】
#   - PUSH差分から 1min OHLCV を再構築
#   - 1min を正本として 3min / 5min を再構築
#   - higher TF 再構築用の最小ソース窓を算出
#   - symbolごとの recent bars 切り詰め
#
# 【設計方針】
#   - 起動時は full rebuild を避け、差分再構築を優先
#   - 1min は PUSH から minute bucket 単位で生成
#   - 3min / 5min は resample_summary_df に委譲
#   - 本モジュールは「再構築」に責務を限定
#
# 【依存】
#   - trading.summary.recovery.helpers
#   - trading.summary.recovery.loaders
#   - trading.summary.resample
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from .helpers import (
    ensure_dataframe,
    merge_summary_frames_with_priority,
    normalize_datetime_columns,
    repair_ohlc_alias,
    safe_get_series,
)
from .loaders import normalize_push_df

logger = logging.getLogger(__name__)

# 起動時や差分再計算時に保持する 1min の recent bars 数
RECENT_RECALC_BARS_1M = 300


# ============================================================
# Generic helpers
# ============================================================

def _sanitize_price_series(s: pd.Series | None) -> pd.Series:
    if s is None:
        return pd.Series(dtype="float64")
    try:
        out = pd.to_numeric(s, errors="coerce")
        out = out.replace([np.inf, -np.inf], np.nan)
        out = out.mask(out <= 0, np.nan)
        return out
    except Exception:
        logger.debug("[summary.recovery.rebuilders] sanitize price failed", exc_info=True)
        return pd.Series(dtype="float64")


def _sanitize_volume_series(s: pd.Series | None) -> pd.Series:
    if s is None:
        return pd.Series(dtype="float64")
    try:
        out = pd.to_numeric(s, errors="coerce")
        out = out.replace([np.inf, -np.inf], np.nan)
        return out
    except Exception:
        logger.debug("[summary.recovery.rebuilders] sanitize volume failed", exc_info=True)
        return pd.Series(dtype="float64")


def _finalize_rebuilt_1m(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_dataframe(df)
    if out.empty:
        return out

    try:
        out = repair_ohlc_alias(out)
        out = normalize_datetime_columns(out, interval=1)

        if "open" in out.columns:
            out["open"] = _sanitize_price_series(safe_get_series(out, "open"))
        if "high" in out.columns:
            out["high"] = _sanitize_price_series(safe_get_series(out, "high"))
        if "low" in out.columns:
            out["low"] = _sanitize_price_series(safe_get_series(out, "low"))
        if "close" in out.columns:
            out["close"] = _sanitize_price_series(safe_get_series(out, "close"))
        if "volume" in out.columns:
            out["volume"] = _sanitize_volume_series(safe_get_series(out, "volume"))

        # OHLC の最低限の妥当性
        if {"open", "high", "low", "close"}.issubset(out.columns):
            open_s = _sanitize_price_series(safe_get_series(out, "open"))
            high_s = _sanitize_price_series(safe_get_series(out, "high"))
            low_s = _sanitize_price_series(safe_get_series(out, "low"))
            close_s = _sanitize_price_series(safe_get_series(out, "close"))

            valid = (
                open_s.notna()
                & high_s.notna()
                & low_s.notna()
                & close_s.notna()
                & (high_s >= low_s)
                & (high_s >= open_s)
                & (high_s >= close_s)
                & (low_s <= open_s)
                & (low_s <= close_s)
            )
            before = len(out)
            out = out.loc[valid.fillna(False)].copy()
            dropped = before - len(out)
            if dropped > 0:
                logger.warning(
                    "[summary.recovery.rebuilders] invalid rebuilt 1m rows removed dropped=%d before=%d after=%d",
                    dropped, before, len(out)
                )

        if {"symbol", "datetime"}.issubset(out.columns):
            out = (
                out.sort_values(["symbol", "datetime"], kind="stable")
                .drop_duplicates(["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )

        return out

    except Exception:
        logger.exception("[summary.recovery.rebuilders] finalize rebuilt 1m failed")
        return pd.DataFrame()


# ============================================================
# Window calculation for higher TF rebuild
# ============================================================

def calc_higher_tf_source_window(
    *,
    interval: int,
    last_higher_dt: pd.Timestamp | None,
    now_dt: pd.Timestamp | None,
    warmup_bars: int,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """
    higher TF 再構築用の最小 1min ソース窓を返す。

    例:
      - 3min, warmup_bars=90 なら 270分ぶん
      - 5min, warmup_bars=90 なら 450分ぶん

    起動時は full day を読むのではなく、
    last_higher_dt の少し手前から必要本数だけ読む。
    """
    try:
        if now_dt is None or pd.isna(now_dt):
            now_dt = pd.Timestamp.now().tz_localize(None)

        if last_higher_dt is None or pd.isna(last_higher_dt):
            start_dt = now_dt - pd.Timedelta(minutes=int(interval) * int(warmup_bars))
            return start_dt, now_dt

        last_higher_dt = pd.to_datetime(last_higher_dt, errors="coerce")
        if pd.isna(last_higher_dt):
            start_dt = now_dt - pd.Timedelta(minutes=int(interval) * int(warmup_bars))
            return start_dt, now_dt

        start_dt = last_higher_dt - pd.Timedelta(minutes=int(interval) * int(warmup_bars))
        return start_dt, now_dt

    except Exception:
        logger.exception(
            "[summary.recovery.rebuilders] calc_higher_tf_source_window failed interval=%s last_higher_dt=%s now_dt=%s warmup_bars=%s",
            interval, last_higher_dt, now_dt, warmup_bars
        )
        return None, now_dt


# ============================================================
# 1min rebuild from PUSH
# ============================================================

def rebuild_1min_from_push(df_push: pd.DataFrame) -> pd.DataFrame:
    """
    PUSH差分から 1min OHLCV を再構築する。

    入力想定:
      - symbol
      - tick_time
      - price
      - cum_volume (あれば)
      - symbolname (あれば)
    """
    if df_push is None or df_push.empty:
        return pd.DataFrame()

    df = normalize_push_df(df_push)
    if df.empty:
        return pd.DataFrame()

    try:
        df["minute"] = pd.to_datetime(df["tick_time"], errors="coerce").dt.floor("min")
        df = df.dropna(subset=["symbol", "minute", "price"]).copy()
        if df.empty:
            return pd.DataFrame()

        grouped = (
            df.groupby(["symbol", "minute"], as_index=False)
            .agg(
                open=("price", "first"),
                high=("price", "max"),
                low=("price", "min"),
                close=("price", "last"),
                symbolname=("symbolname", "last"),
                last_cum_volume=("cum_volume", "last"),
                tick_count=("price", "size"),
            )
            .sort_values(["symbol", "minute"], kind="stable")
            .reset_index(drop=True)
        )

        grouped["datetime"] = pd.to_datetime(grouped["minute"], errors="coerce")
        grouped["volume"] = _sanitize_volume_series(safe_get_series(grouped, "last_cum_volume")).fillna(0)
        grouped["interval"] = 1
        grouped["source"] = "summary_recovery_push_1m"

        # alias列も後段の互換性のために持たせる
        grouped["open_price"] = grouped["open"]
        grouped["high_price"] = grouped["high"]
        grouped["low_price"] = grouped["low"]
        grouped["close_price"] = grouped["close"]
        grouped["price"] = grouped["close"]
        grouped["current_price"] = grouped["close"]
        grouped["CurrentPrice"] = grouped["close"]
        grouped["last_price"] = grouped["close"]
        grouped["LastPrice"] = grouped["close"]
        grouped["trading_volume"] = grouped["volume"]
        grouped["TradingVolume"] = grouped["volume"]

        out = _finalize_rebuilt_1m(grouped)

        logger.info(
            "[summary.recovery.rebuilders] rebuild_1min_from_push done rows=%d symbols=%d tick_min=%s tick_max=%s",
            len(out),
            int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
            df["tick_time"].min() if "tick_time" in df.columns and not df.empty else None,
            df["tick_time"].max() if "tick_time" in df.columns and not df.empty else None,
        )
        return out

    except Exception:
        logger.exception("[summary.recovery.rebuilders] rebuild_1min_from_push failed")
        return pd.DataFrame()


# ============================================================
# Higher timeframe rebuild from 1min
# ============================================================

def rebuild_higher_tf_from_1m(df_1m: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    1min summary を正本として higher TF を再構築する。
    interval は 3 または 5 を想定。
    """
    if int(interval) not in (3, 5):
        logger.error("[summary.recovery.rebuilders] unsupported interval=%s", interval)
        return pd.DataFrame()

    base = normalize_datetime_columns(df_1m, interval=1)
    base = repair_ohlc_alias(base)

    if base.empty:
        return pd.DataFrame()

    try:
        from trading.summary.resample import resample_summary_df

        # resample 前の最低限の列チェック
        required_cols = {"symbol", "datetime"}
        if not required_cols.issubset(base.columns):
            logger.error(
                "[summary.recovery.rebuilders] rebuild_higher_tf_from_1m missing required columns interval=%s cols=%s",
                interval,
                list(base.columns),
            )
            return pd.DataFrame()

        out = resample_summary_df(base, interval=int(interval))
        out = normalize_datetime_columns(out, interval=int(interval))
        out = repair_ohlc_alias(out)

        # 後段互換用
        if not out.empty:
            if "interval" not in out.columns:
                out["interval"] = int(interval)
            if "source" not in out.columns:
                out["source"] = f"summary_recovery_resample_{interval}m"

            for src, dst in (
                ("open", "open_price"),
                ("high", "high_price"),
                ("low", "low_price"),
                ("close", "close_price"),
                ("close", "price"),
                ("close", "current_price"),
                ("close", "CurrentPrice"),
                ("close", "last_price"),
                ("close", "LastPrice"),
                ("volume", "trading_volume"),
                ("volume", "TradingVolume"),
            ):
                if src in out.columns and dst not in out.columns:
                    out[dst] = out[src]

            if {"symbol", "datetime"}.issubset(out.columns):
                out = (
                    out.sort_values(["symbol", "datetime"], kind="stable")
                    .drop_duplicates(["symbol", "datetime"], keep="last")
                    .reset_index(drop=True)
                )

        logger.info(
            "[summary.recovery.rebuilders] rebuild_higher_tf_from_1m done interval=%s rows=%d symbols=%d src_rows=%d",
            interval,
            len(out),
            int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
            len(base),
        )
        return out

    except Exception:
        logger.exception("[summary.recovery.rebuilders] rebuild_higher_tf_from_1m failed interval=%s", interval)
        return pd.DataFrame()


# ============================================================
# Recent bars trim
# ============================================================

def trim_recent_bars(df: pd.DataFrame, bars: int = RECENT_RECALC_BARS_1M) -> pd.DataFrame:
    """
    symbolごとに最新 bars 本だけ残す。
    差分マージ後の再保存サイズを抑える用途。
    """
    out = normalize_datetime_columns(df, interval=1)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return out

    try:
        out = (
            out.sort_values(["symbol", "datetime"], kind="stable")
            .groupby("symbol", group_keys=False)
            .tail(int(bars))
            .reset_index(drop=True)
        )

        logger.info(
            "[summary.recovery.rebuilders] trim_recent_bars done bars=%d rows=%d symbols=%d",
            bars,
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns and not out.empty else 0,
        )
        return out

    except Exception:
        logger.exception("[summary.recovery.rebuilders] trim_recent_bars failed bars=%s", bars)
        return out


__all__ = [
    "RECENT_RECALC_BARS_1M",
    "calc_higher_tf_source_window",
    "rebuild_1min_from_push",
    "rebuild_higher_tf_from_1m",
    "trim_recent_bars",

]