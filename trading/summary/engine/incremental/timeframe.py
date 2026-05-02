# ============================================================
# File   : trading/summary/engine/incremental/timeframe.py
# Version: Ver1.0-INCREMENTAL-TIMEFRAME
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from utils.df_guard.extractor import extract_latest_by_symbol

from .common import now_naive, safe_log_error

logger = logging.getLogger(__name__)


def normalize_intraday_bar_times(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        out = df.copy()
        interval = int(interval)

        if "datetime" not in out.columns:
            return out

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        out = out.dropna(subset=["datetime"])
        if out.empty:
            return out

        if interval <= 1:
            base_dt = out["datetime"].dt.floor("1min")
            end_dt = base_dt + pd.Timedelta(minutes=1)
        else:
            base_dt = out["datetime"].dt.floor(f"{interval}min")
            end_dt = base_dt + pd.Timedelta(minutes=interval)

        out["datetime"] = base_dt
        out["date"] = base_dt.dt.strftime("%Y-%m-%d")
        out["start_time"] = base_dt.dt.strftime("%H:%M:%S")
        out["end_time"] = end_dt.dt.strftime("%H:%M:%S")
        out["time"] = base_dt.dt.strftime("%H:%M:%S")
        out["time_range"] = base_dt.dt.strftime("%H:%M") + "-" + end_dt.dt.strftime("%H:%M")
        return out.reset_index(drop=True)

    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] normalize intraday bar times failed interval=%s", interval, exc=e)
        return df


def drop_future_rows(df: pd.DataFrame, *, tolerance_seconds: int = 60) -> pd.DataFrame:
    if df is None or df.empty or "datetime" not in df.columns:
        return df

    try:
        out = df.copy()
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        now = now_naive()
        cutoff = now + dt.timedelta(seconds=tolerance_seconds)

        before = len(out)
        out = out.loc[out["datetime"].isna() | (out["datetime"] <= cutoff)].copy()

        dropped = before - len(out)
        if dropped > 0:
            logger.warning(
                "[INCREMENTAL SUMMARY] future rows dropped=%s cutoff=%s sample_future_max=%s",
                dropped,
                cutoff,
                pd.to_datetime(df["datetime"], errors="coerce").max() if "datetime" in df.columns else None,
            )
        return out.reset_index(drop=True)

    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] drop future rows failed", exc=e)
        return df


def latest_row_stale_seconds(interval: int) -> int:
    interval = int(interval)
    if interval <= 1:
        return 180
    if interval <= 3:
        return 600
    if interval <= 5:
        return 900
    return 1200


def extract_latest_timeframe(df: pd.DataFrame, interval: int = 1) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        if "datetime" not in df.columns:
            return df

        if "symbol" not in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.dropna(subset=["datetime"])
            return df.reset_index(drop=True)

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        try:
            df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        df["symbol"] = df["symbol"].astype(str)
        df = df.dropna(subset=["symbol", "datetime"])

        if df.empty:
            return df

        try:
            df_latest = extract_latest_by_symbol(df.copy())
        except Exception:
            df_latest = (
                df.sort_values(["symbol", "datetime"], kind="stable")
                .groupby("symbol", as_index=False, group_keys=False)
                .tail(1)
                .reset_index(drop=True)
            )

        if df_latest.empty:
            return df_latest

        now = now_naive()
        stale_sec = latest_row_stale_seconds(interval)

        try:
            age_sec = (now - df_latest["datetime"]).dt.total_seconds()
            keep_mask = age_sec <= stale_sec
            before = len(df_latest)
            df_latest = df_latest.loc[keep_mask.fillna(False)].copy().reset_index(drop=True)
            logger.info(
                "[INCREMENTAL SUMMARY] latest-by-symbol interval=%s rows=%s -> %s stale_sec=%s now=%s",
                interval,
                before,
                len(df_latest),
                stale_sec,
                now,
            )
        except Exception:
            logger.debug("[INCREMENTAL SUMMARY] latest stale filter failed", exc_info=True)

        return df_latest.reset_index(drop=True)

    except Exception as e:
        safe_log_error("latest timeframe extraction failed", exc=e)
        return df


def dedupe_prefer_completed_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        x = df.copy()

        if "symbol" not in x.columns:
            return x

        x["symbol"] = x["symbol"].astype(str)

        if "datetime" in x.columns:
            x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
            try:
                x["datetime"] = x["datetime"].dt.tz_localize(None)
            except Exception:
                pass

        complete_score = pd.Series(0, index=x.index, dtype="int64")

        if "symbolname" in x.columns:
            name_ok = x["symbolname"].notna() & x["symbolname"].astype(str).str.strip().ne("")
            complete_score += name_ok.astype(int) * 10

        for c, w in [
            ("score", 5),
            ("mtf", 4),
            ("score_mtf", 4),
            ("mtf_score", 4),
            ("macd", 3),
            ("signal", 3),
            ("score_buy", 2),
            ("score_sell", 2),
            ("rsi", 1),
            ("hist", 1),
        ]:
            if c in x.columns:
                s = pd.to_numeric(x[c], errors="coerce")
                complete_score += s.notna().astype(int) * w

        x["_complete_score"] = complete_score

        sort_cols = ["symbol", "_complete_score"]
        ascending = [True, False]

        if "datetime" in x.columns:
            sort_cols.append("datetime")
            ascending.append(False)

        x = x.sort_values(sort_cols, ascending=ascending, kind="stable")

        before = len(x)
        x = x.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
        removed = before - len(x)

        if removed > 0:
            logger.info(
                "[INCREMENTAL SUMMARY] dedupe prefer completed rows removed=%s remaining=%s symbols=%s",
                removed,
                len(x),
                x["symbol"].nunique() if "symbol" in x.columns else len(x),
            )

        return x.drop(columns=["_complete_score"], errors="ignore")

    except Exception as e:
        safe_log_error("[INCREMENTAL SUMMARY] dedupe prefer completed rows failed", exc=e)
        return df