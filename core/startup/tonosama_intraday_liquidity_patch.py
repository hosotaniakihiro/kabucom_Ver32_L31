# ============================================================
# File   : core/startup/tonosama_intraday_liquidity_patch.py
# Version: V1.0-TONOSAMA-INTRADAY-LIQUIDITY-FOLLOWER-GUARD
# ------------------------------------------------------------
# 目的:
#   殿様イナゴの候補から、日中出来高・売買代金が少ない銘柄を除外する。
#
# 方針:
#   - 「直近だけ出来高急増」ではなく、当日累計で資金が入っている銘柄を優先する。
#   - 後からイナゴが付いてきやすいように、以下を満たすものだけ残す。
#       1) 当日累計出来高
#       2) 当日累計売買代金
#       3) 出来高が発生した分足数
#       4) 直近 3m/5m または 1m の出来高
#   - trading.entry.tonosama.volume_surge.build_scalping_feature_df() をwrapする。
#   - main.py の起動patchに追加しなくても、tonosama.config から install() される想定。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _now_naive() -> dt.datetime:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive().replace(tzinfo=None)
    except Exception:
        return dt.datetime.now()


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    try:
        for n in names:
            if n in df.columns:
                return n
    except Exception:
        pass
    return None


def _load_1m_today_stats() -> pd.DataFrame:
    """当日1分足から、日中の累計出来高・売買代金・活動分足数を作る。"""
    try:
        from trading.entry.tonosama.summary_loader import load_merged_summary, normalize_summary_base

        raw = load_merged_summary(1)
        x = normalize_summary_base(raw, interval=1)
        if x is None or x.empty:
            return pd.DataFrame()
        if "symbol" not in x.columns or "datetime" not in x.columns:
            return pd.DataFrame()

        x = x.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["datetime"])
        if x.empty:
            return pd.DataFrame()

        today = pd.Timestamp(_now_naive().date())
        x = x[x["datetime"] >= today].copy()
        if x.empty:
            return pd.DataFrame()

        volume_col = _first_existing(x, ["volume", "Volume", "latest_volume", "volume_1m"])
        close_col = _first_existing(x, ["close", "close_price", "current_price", "price", "close_1m"])
        if volume_col is None:
            return pd.DataFrame()

        x["_day_volume_src"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0).clip(lower=0.0)
        if close_col:
            x["_day_close_src"] = pd.to_numeric(x[close_col], errors="coerce").fillna(0.0).clip(lower=0.0)
        else:
            x["_day_close_src"] = 0.0
        x["_day_turnover_src"] = x["_day_volume_src"] * x["_day_close_src"]
        x["_active_min_src"] = (x["_day_volume_src"] > 0).astype(int)

        g = x.groupby("symbol", as_index=False).agg(
            day_volume=("_day_volume_src", "sum"),
            day_turnover=("_day_turnover_src", "sum"),
            active_volume_minutes=("_active_min_src", "sum"),
            max_1m_volume=("_day_volume_src", "max"),
            avg_1m_volume=("_day_volume_src", "mean"),
            last_1m_dt=("datetime", "max"),
        )
        for c in ["day_volume", "day_turnover", "active_volume_minutes", "max_1m_volume", "avg_1m_volume"]:
            g[c] = pd.to_numeric(g[c], errors="coerce").fillna(0.0)
        return g
    except Exception:
        logger.exception("[TONOSAMA INTRADAY LIQ] build day stats failed")
        return pd.DataFrame()


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def _apply_intraday_liquidity_filter(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if not _env_bool("TONOSAMA_INTRADAY_LIQUIDITY_GUARD", True):
        return df

    x = df.copy()
    stats = _load_1m_today_stats()
    if not stats.empty:
        x["symbol"] = x["symbol"].astype(str)
        stats["symbol"] = stats["symbol"].astype(str)
        x = x.merge(stats, on="symbol", how="left")
    else:
        for c in ["day_volume", "day_turnover", "active_volume_minutes", "max_1m_volume", "avg_1m_volume"]:
            if c not in x.columns:
                x[c] = 0.0

    for c in ["day_volume", "day_turnover", "active_volume_minutes", "max_1m_volume", "avg_1m_volume", "volume_3m", "volume_5m", "_latest_volume"]:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce").fillna(0.0)

    min_day_volume = _env_float("TONOSAMA_MIN_DAY_VOLUME", 100000.0)
    min_day_turnover = _env_float("TONOSAMA_MIN_DAY_TURNOVER", 100000000.0)  # 1億円
    min_active_minutes = _env_float("TONOSAMA_MIN_ACTIVE_VOLUME_MINUTES", 8.0)
    min_recent_3m_volume = _env_float("TONOSAMA_MIN_RECENT_3M_VOLUME", 50000.0)
    min_recent_5m_volume = _env_float("TONOSAMA_MIN_RECENT_5M_VOLUME", 80000.0)
    min_latest_volume = _env_float("TONOSAMA_MIN_LATEST_VOLUME", 50000.0)

    recent_ok = (
        (_num(x, "volume_3m") >= min_recent_3m_volume)
        | (_num(x, "volume_5m") >= min_recent_5m_volume)
        | (_num(x, "_latest_volume") >= min_latest_volume)
        | (_num(x, "max_1m_volume") >= min_latest_volume)
    )
    day_ok = (
        (_num(x, "day_volume") >= min_day_volume)
        & (_num(x, "day_turnover") >= min_day_turnover)
        & (_num(x, "active_volume_minutes") >= min_active_minutes)
    )

    before = len(x)
    kept = x[day_ok & recent_ok].copy()
    dropped = before - len(kept)
    if dropped > 0:
        sample_cols = [
            "symbol", "symbolname", "close", "day_volume", "day_turnover", "active_volume_minutes",
            "volume_3m", "volume_5m", "_latest_volume", "max_1m_volume", "_max_volume_surge_ratio", "_max_price_change_pct",
        ]
        sample = kept.head(8)[[c for c in sample_cols if c in kept.columns]].to_dict("records") if not kept.empty else []
        drop_sample = x.loc[~(day_ok & recent_ok)].head(8)[[c for c in sample_cols if c in x.columns]].to_dict("records")
        logger.warning(
            "[TONOSAMA INTRADAY LIQ] filtered before=%s after=%s dropped=%s thresholds=%s kept_head=%s dropped_head=%s",
            before,
            len(kept),
            dropped,
            {
                "MIN_DAY_VOLUME": min_day_volume,
                "MIN_DAY_TURNOVER": min_day_turnover,
                "MIN_ACTIVE_VOLUME_MINUTES": min_active_minutes,
                "MIN_RECENT_3M_VOLUME": min_recent_3m_volume,
                "MIN_RECENT_5M_VOLUME": min_recent_5m_volume,
                "MIN_LATEST_VOLUME": min_latest_volume,
            },
            sample,
            drop_sample,
        )
    else:
        logger.info("[TONOSAMA INTRADAY LIQ] pass all rows=%s", before)
    return kept.reset_index(drop=True)


def install() -> bool:
    global _PATCHED, _ORIGINAL
    if _PATCHED:
        return True
    try:
        import trading.entry.tonosama.volume_surge as volume_surge

        fn = getattr(volume_surge, "build_scalping_feature_df", None)
        if not callable(fn):
            logger.warning("[TONOSAMA INTRADAY LIQ] build_scalping_feature_df unavailable")
            return False
        if getattr(fn, "_tonosama_intraday_liquidity_guard", False):
            _PATCHED = True
            return True

        _ORIGINAL = fn

        def _wrapped_build_scalping_feature_df(*args: Any, **kwargs: Any) -> pd.DataFrame:
            base = _ORIGINAL(*args, **kwargs)
            return _apply_intraday_liquidity_filter(base)

        _wrapped_build_scalping_feature_df._tonosama_intraday_liquidity_guard = True  # type: ignore[attr-defined]
        _wrapped_build_scalping_feature_df._original = _ORIGINAL  # type: ignore[attr-defined]
        volume_surge.build_scalping_feature_df = _wrapped_build_scalping_feature_df
        _PATCHED = True
        logger.warning("[TONOSAMA INTRADAY LIQ] installed V1.0")
        return True
    except Exception:
        logger.exception("[TONOSAMA INTRADAY LIQ] install failed")
        return False


__all__ = ["install"]
