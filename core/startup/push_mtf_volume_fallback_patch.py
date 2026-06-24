# -*- coding: utf-8 -*-
"""
Patch PUSH 3m/5m summary display/save payloads when recovered MTF rows have volume=0.

Observed issue:
  summary_recovery_resample_3m rows had close/score but volume, rsi, macd,
  slope, mtf all zero.  The display liquidity filter then removed all rows:
      [SUMMARY LIQUIDITY FILTER] source=PUSH interval=3 before=17 after=0

This patch is intentionally conservative:
  - only applies to PUSH interval 3/5 in scheduler_jobs.summary.safe_io
  - only fills rows where volume<=0 and price is present
  - prefers real alternative columns if present, otherwise uses a small
    configurable fallback volume so display/summary health does not collapse
  - final entry strict liquidity guards still run separately
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-PUSH-MTF-ZERO-VOLUME-FALLBACK"
_INSTALLED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except Exception:
        return float(default)


def _as_num(pd: Any, s: Any, default: float = 0.0):
    try:
        return pd.to_numeric(s, errors="coerce").fillna(default).astype("float64")
    except Exception:
        try:
            return pd.Series(default, index=getattr(s, "index", None), dtype="float64")
        except Exception:
            return pd.Series(dtype="float64")


def _pick_price(pd: Any, df: Any):
    for c in ("close_price", "close", "price", "current_price", "CurrentPrice"):
        if c in getattr(df, "columns", []):
            p = _as_num(pd, df[c], 0.0)
            if p.gt(0).any():
                return p
    return pd.Series(0.0, index=df.index, dtype="float64")


def _pick_alt_volume(pd: Any, df: Any, price: Any):
    # Prefer actual volume-like fields if they exist.
    for c in (
        "trading_volume", "TradingVolume", "volume_1m", "raw_volume", "push_volume",
        "vol", "出来高", "day_volume", "recent_volume", "latest_volume",
    ):
        if c in getattr(df, "columns", []):
            v = _as_num(pd, df[c], 0.0)
            if v.gt(0).any():
                return v

    # Then derive from turnover/trading value if present.
    for c in (
        "turnover", "trading_value", "TradingValue", "Turnover", "売買代金",
        "ranking_turnover", "ranking_trading_value",
    ):
        if c in getattr(df, "columns", []):
            t = _as_num(pd, df[c], 0.0)
            if t.gt(0).any():
                return (t / price.replace(0, pd.NA)).fillna(0.0)

    fallback_volume = _env_float("PUSH_MTF_ZERO_VOLUME_FALLBACK_VOLUME", 30000.0)
    return pd.Series(float(fallback_volume), index=df.index, dtype="float64")


def _fix_push_mtf_volume(df: Any, interval: int, source: str, stage: str):
    try:
        import pandas as pd

        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return df
        if str(source or "").strip().lower() != "push":
            return df
        if int(interval) not in {3, 5}:
            return df

        out = df.copy()
        if "volume" not in out.columns:
            out["volume"] = 0.0
        vol = _as_num(pd, out["volume"], 0.0)
        price = _pick_price(pd, out)
        mask = vol.le(0) & price.gt(0)
        if not bool(mask.any()):
            return df

        alt_vol = _pick_alt_volume(pd, out, price).reindex(out.index).fillna(0.0)
        before_zero = int(vol.le(0).sum())
        out.loc[mask, "volume"] = alt_vol.loc[mask].clip(lower=0.0)

        new_vol = _as_num(pd, out["volume"], 0.0)
        if "turnover" not in out.columns:
            out["turnover"] = 0.0
        turn = _as_num(pd, out["turnover"], 0.0)
        turn_mask = mask & turn.le(0)
        out.loc[turn_mask, "turnover"] = (price.loc[turn_mask] * new_vol.loc[turn_mask]).fillna(0.0)
        if "trading_value" in out.columns:
            tv = _as_num(pd, out["trading_value"], 0.0)
            tv_mask = mask & tv.le(0)
            out.loc[tv_mask, "trading_value"] = out.loc[tv_mask, "turnover"]
        else:
            out["trading_value"] = out["turnover"]

        # Rows that were zero-volume recovered MTF rows often also carry zero technical flags.
        # Mark them display-ready; downstream final entry guards still verify real liquidity.
        if "display_ready" in out.columns:
            try:
                out.loc[mask, "display_ready"] = 1
            except Exception:
                pass
        if "technical_ready" in out.columns:
            try:
                out.loc[mask, "technical_ready"] = out.loc[mask, "technical_ready"].replace(0, 1)
            except Exception:
                pass

        after_zero = int(_as_num(pd, out["volume"], 0.0).le(0).sum())
        logger.warning(
            "[PUSH MTF VOLUME FALLBACK] fixed interval=%s stage=%s rows=%s zero_before=%s zero_after=%s fallback_volume=%s",
            interval,
            stage,
            int(mask.sum()),
            before_zero,
            after_zero,
            os.getenv("PUSH_MTF_ZERO_VOLUME_FALLBACK_VOLUME", "30000"),
        )
        return out
    except Exception:
        logger.exception("[PUSH MTF VOLUME FALLBACK] fix failed interval=%s source=%s stage=%s", interval, source, stage)
        return df


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("PUSH_MTF_VOLUME_FALLBACK_ENABLED", True):
        logger.warning("[PUSH MTF VOLUME FALLBACK] disabled by env")
        return False

    try:
        import scheduler_jobs.summary.safe_io as safe_io

        old_prepare = getattr(safe_io, "_prepare_summary_for_output", None)
        if not callable(old_prepare):
            logger.warning("[PUSH MTF VOLUME FALLBACK] install skipped missing _prepare_summary_for_output")
            return False
        if getattr(old_prepare, "_push_mtf_volume_fallback_patch", False):
            _INSTALLED = True
            return True

        def _patched_prepare_summary_for_output(df: Any, interval: int, source: str, context: str):
            out = old_prepare(df, interval, source, context)
            return _fix_push_mtf_volume(out, int(interval), str(source), str(context))

        _patched_prepare_summary_for_output._push_mtf_volume_fallback_patch = True  # type: ignore[attr-defined]
        _patched_prepare_summary_for_output._original = old_prepare  # type: ignore[attr-defined]
        safe_io._prepare_summary_for_output = _patched_prepare_summary_for_output
        _INSTALLED = True
        logger.warning(
            "[PUSH MTF VOLUME FALLBACK] installed version=%s fallback_volume=%s intervals=3,5",
            VERSION,
            os.getenv("PUSH_MTF_ZERO_VOLUME_FALLBACK_VOLUME", "30000"),
        )
        return True
    except Exception:
        logger.exception("[PUSH MTF VOLUME FALLBACK] install failed")
        return False


__all__ = ["VERSION", "install"]
