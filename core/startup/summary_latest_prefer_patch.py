# -*- coding: utf-8 -*-
"""
Patch summary_controller merged-cache selection so stale history does not beat fresh PUSH latest rows.

Problem observed:
    At 10:50, push summary latest_dt stayed around 09:46.  The 1-minute merged
    cache preferred rich history rows even though the current latest rows were newer.

Policy:
    For interval=1, if df_latest is meaningfully newer than df_hist, prefer/latest-merge
    latest rows and do not let stale history define the merged summary latest_dt.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-LATEST-PREFER-WHEN-HISTORY-STALE"
_INSTALLED = False
_ORIGINAL = None


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _safe_symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        s = df["symbol"].fillna("").astype(str).str.strip()
        s = s[(s != "") & (s.str.lower() != "nan") & (s.str.lower() != "none")]
        return int(s.nunique())
    except Exception:
        return 0


def _safe_latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "end_time", "snapshot_time", "tick_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                s = s.dropna()
                if not s.empty:
                    return s.max()
    except Exception:
        return None
    return None


def _numeric_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        best = 0
        for c in cols:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce").fillna(0)
                best = max(best, int((s != 0).sum()))
        return best
    except Exception:
        return 0


def _numeric_nonnull(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        best = 0
        for c in cols:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                best = max(best, int(s.notna().sum()))
        return best
    except Exception:
        return 0


def _latest_is_usable(df: pd.DataFrame, min_symbols: int) -> bool:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return False
        symbols = _safe_symbol_count(df)
        close_nonnull = _numeric_nonnull(df, ("close", "close_price", "price", "current_price", "last_price"))
        score_nonzero = _numeric_nonzero(df, ("score", "score_total", "final_score", "display_score", "score_buy", "score_sell"))
        # score may be zero for quiet symbols, but close/current price must exist.
        return symbols >= int(min_symbols) and close_nonnull >= max(1, min_symbols // 2) and (score_nonzero >= 0)
    except Exception:
        return False


def _merge_latest_over_history(cc, interval: int, hist: pd.DataFrame, latest: pd.DataFrame, normalize_fn: Callable[[pd.DataFrame], pd.DataFrame]) -> pd.DataFrame:
    """Keep history rows, but force latest row per symbol to be from df_latest when newer."""
    frames = []
    try:
        h = normalize_fn(hist)
        if isinstance(h, pd.DataFrame) and not h.empty:
            frames.append(h)
    except Exception:
        pass
    try:
        l = normalize_fn(latest)
        if isinstance(l, pd.DataFrame) and not l.empty:
            frames.append(l)
    except Exception:
        l = pd.DataFrame()
    if not frames:
        return pd.DataFrame()
    out = cc.concat_frames(frames, normalize_fn=normalize_fn)
    out = cc.dedupe_symbol_datetime(out, normalize_fn=normalize_fn)
    out = cc.limit_history_rows_per_symbol(out, interval, normalize_fn=normalize_fn)
    out = cc.attach_display_ready(out)
    return out


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    try:
        import trading.summary.controller_cache as cc

        _ORIGINAL = getattr(cc, "choose_merged_cache_payload", None)
        if not callable(_ORIGINAL):
            logger.warning("[SUMMARY LATEST PREFER PATCH] original choose_merged_cache_payload missing")
            return False

        def patched_choose_merged_cache_payload(interval: int, df_hist: pd.DataFrame, df_latest: pd.DataFrame, normalize_fn: Callable[[pd.DataFrame], pd.DataFrame]) -> pd.DataFrame:
            try:
                interval_i = int(interval)
                if interval_i != 1:
                    return _ORIGINAL(interval, df_hist, df_latest, normalize_fn)

                lag_sec = _env_int("SUMMARY_FORCE_LATEST_WHEN_HISTORY_LAG_SEC", 180)
                min_symbols = _env_int("SUMMARY_FORCE_LATEST_MIN_SYMBOLS", 20)

                hist = normalize_fn(df_hist)
                latest = normalize_fn(df_latest)
                hist_dt = _safe_latest_dt(hist)
                latest_dt = _safe_latest_dt(latest)
                hist_symbols = _safe_symbol_count(hist)
                latest_symbols = _safe_symbol_count(latest)
                latest_usable = _latest_is_usable(latest, min_symbols)

                force_latest = False
                delta_sec = None
                if hist_dt is not None and latest_dt is not None:
                    try:
                        delta_sec = float((latest_dt - hist_dt).total_seconds())
                        force_latest = delta_sec >= float(lag_sec) and latest_usable
                    except Exception:
                        force_latest = False
                elif latest_dt is not None and latest_usable:
                    force_latest = True

                if force_latest:
                    payload = _merge_latest_over_history(cc, interval_i, hist, latest, normalize_fn)
                    logger.warning(
                        "[SUMMARY LATEST PREFER PATCH] force latest for interval=%s hist_dt=%s latest_dt=%s delta_sec=%s hist_symbols=%s latest_symbols=%s rows=%s payload_latest_dt=%s lag_sec=%s",
                        interval_i,
                        hist_dt,
                        latest_dt,
                        delta_sec,
                        hist_symbols,
                        latest_symbols,
                        len(payload) if isinstance(payload, pd.DataFrame) else 0,
                        _safe_latest_dt(payload),
                        lag_sec,
                    )
                    return payload

                return _ORIGINAL(interval, df_hist, df_latest, normalize_fn)
            except Exception:
                logger.exception("[SUMMARY LATEST PREFER PATCH] patched choose failed interval=%s", interval)
                return _ORIGINAL(interval, df_hist, df_latest, normalize_fn)

        cc.choose_merged_cache_payload = patched_choose_merged_cache_payload

        # summary_controller imports the function directly, so patch that binding too if loaded.
        try:
            import trading.summary.summary_controller as sc
            sc.choose_merged_cache_payload = patched_choose_merged_cache_payload
        except Exception:
            logger.debug("[SUMMARY LATEST PREFER PATCH] summary_controller binding patch skipped", exc_info=True)

        _INSTALLED = True
        logger.warning(
            "[SUMMARY LATEST PREFER PATCH] installed version=%s lag_sec=%s min_symbols=%s",
            VERSION,
            _env_int("SUMMARY_FORCE_LATEST_WHEN_HISTORY_LAG_SEC", 180),
            _env_int("SUMMARY_FORCE_LATEST_MIN_SYMBOLS", 20),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY LATEST PREFER PATCH] install failed")
        return False


__all__ = ["VERSION", "install"]
