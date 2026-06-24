# -*- coding: utf-8 -*-
"""
Patch summary_controller merged-cache selection so stale history does not beat fresh PUSH latest rows.

Problem observed:
    At 10:50, push summary latest_dt stayed around 09:46.  The 1-minute merged
    cache preferred rich history rows even though the current latest rows were newer.

Policy:
    For interval=1/3/5, if df_latest is usable and either:
      * df_latest is newer than df_hist, or
      * df_latest carries a newer realtime/PUSH timestamp than its summary datetime,
    prefer/latest-merge latest rows and do not let stale history define the merged summary latest_dt.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V2-SUMMARY-LATEST-PREFER-REALTIME-PUSH-TIME"
_INSTALLED = False
_ORIGINAL = None

_REALTIME_TIME_COLS = (
    "received_at",
    "recv_at",
    "recv_time",
    "last_recv",
    "last_recv_at",
    "last_received_at",
    "push_received_at",
    "push_time",
    "tick_time",
    "quote_time",
    "current_price_time",
    "CurrentPriceTime",
    "price_time",
    "event_time",
    "updated_at",
    "created_at",
    "timestamp",
    "time_stamp",
)


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


def _to_naive_datetime_series(s) -> pd.Series:
    try:
        out = pd.to_datetime(s, errors="coerce")
        try:
            out = out.dt.tz_localize(None)
        except Exception:
            try:
                out = out.dt.tz_convert(None)
            except Exception:
                pass
        return out
    except Exception:
        return pd.Series(dtype="datetime64[ns]")


def _safe_latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "end_time", "snapshot_time", "tick_time"):
            if c in df.columns:
                s = _to_naive_datetime_series(df[c]).dropna()
                if not s.empty:
                    return s.max()
    except Exception:
        return None
    return None


def _safe_realtime_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        best = None
        for c in _REALTIME_TIME_COLS:
            if c not in df.columns:
                continue
            s = _to_naive_datetime_series(df[c]).dropna()
            if s.empty:
                continue
            mx = s.max()
            if best is None or mx > best:
                best = mx
        return best
    except Exception:
        return None


def _floor_to_interval_min(s: pd.Series, interval: int) -> pd.Series:
    try:
        interval_i = max(1, int(interval))
        out = s.dt.floor(f"{interval_i}min")
        return out
    except Exception:
        try:
            return s.dt.floor("min")
        except Exception:
            return s


def _promote_latest_datetime(latest: pd.DataFrame, interval: int, min_symbols: int) -> pd.DataFrame:
    """Use realtime/PUSH timestamp columns to make latest rows current enough for merge/stale guard.

    This is intentionally conservative:
      * Only runs when latest rows are otherwise usable.
      * Only promotes rows where realtime timestamp is newer than current datetime.
      * Rejects timestamps too far in the future or too old compared with wall clock.
    """
    try:
        if not _latest_is_usable(latest, min_symbols):
            return latest
        if "datetime" not in latest.columns:
            return latest

        max_future_sec = _env_int("SUMMARY_REALTIME_PROMOTE_MAX_FUTURE_SEC", 60)
        max_wall_lag_sec = _env_int("SUMMARY_REALTIME_PROMOTE_MAX_WALL_LAG_SEC", 900)
        min_promote_sec = _env_int("SUMMARY_REALTIME_PROMOTE_MIN_SEC", 60)

        now = pd.Timestamp.now().tz_localize(None)
        base_dt = _to_naive_datetime_series(latest["datetime"])
        best_col = None
        best_max = None
        best_s = None
        for c in _REALTIME_TIME_COLS:
            if c not in latest.columns:
                continue
            s = _to_naive_datetime_series(latest[c])
            valid = s.dropna()
            if valid.empty:
                continue
            mx = valid.max()
            if best_max is None or mx > best_max:
                best_col = c
                best_max = mx
                best_s = s
        if best_s is None or best_max is None:
            return latest

        # Do not trust future timestamps or very old timestamps.
        if best_max > now + pd.Timedelta(seconds=max_future_sec):
            return latest
        if (now - best_max).total_seconds() > float(max_wall_lag_sec):
            return latest

        new_dt = _floor_to_interval_min(best_s, interval)
        delta = (new_dt - base_dt).dt.total_seconds()
        mask = base_dt.notna() & new_dt.notna() & (delta >= float(min_promote_sec))
        if not bool(mask.any()):
            return latest

        out = latest.copy()
        before = base_dt.max()
        out.loc[mask, "datetime"] = new_dt[mask]
        # Keep common aliases aligned if present.
        for c in ("end_time", "snapshot_time"):
            if c in out.columns:
                try:
                    out.loc[mask, c] = new_dt[mask]
                except Exception:
                    pass
        after = _safe_latest_dt(out)
        logger.warning(
            "[SUMMARY LATEST PREFER PATCH] promoted realtime datetime interval=%s col=%s rows=%s before=%s after=%s realtime_max=%s now=%s",
            interval,
            best_col,
            int(mask.sum()),
            before,
            after,
            best_max,
            now,
        )
        return out
    except Exception:
        logger.exception("[SUMMARY LATEST PREFER PATCH] realtime promote failed interval=%s", interval)
        return latest


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
                intervals = {int(x) for x in str(os.environ.get("SUMMARY_FORCE_LATEST_INTERVALS", "1,3,5")).replace(";", ",").split(",") if str(x).strip().isdigit()}
                if interval_i not in intervals:
                    return _ORIGINAL(interval, df_hist, df_latest, normalize_fn)

                lag_sec = _env_int("SUMMARY_FORCE_LATEST_WHEN_HISTORY_LAG_SEC", 120)
                wall_lag_sec = _env_int("SUMMARY_FORCE_LATEST_WALL_LAG_SEC", 180)
                min_symbols = _env_int("SUMMARY_FORCE_LATEST_MIN_SYMBOLS", 20)

                hist = normalize_fn(df_hist)
                latest = normalize_fn(df_latest)
                latest = _promote_latest_datetime(latest, interval_i, min_symbols)

                hist_dt = _safe_latest_dt(hist)
                latest_dt = _safe_latest_dt(latest)
                realtime_dt = _safe_realtime_dt(latest)
                hist_symbols = _safe_symbol_count(hist)
                latest_symbols = _safe_symbol_count(latest)
                latest_usable = _latest_is_usable(latest, min_symbols)

                force_latest = False
                reason = None
                delta_sec = None
                if hist_dt is not None and latest_dt is not None:
                    try:
                        delta_sec = float((latest_dt - hist_dt).total_seconds())
                        force_latest = delta_sec >= float(lag_sec) and latest_usable
                        if force_latest:
                            reason = "latest_newer_than_history"
                    except Exception:
                        force_latest = False
                elif latest_dt is not None and latest_usable:
                    force_latest = True
                    reason = "latest_only"

                # Even when hist/latest have the same summary datetime, if realtime PUSH time is current
                # while summary datetime lags wall clock, prefer latest so stale history cannot dominate.
                if not force_latest and latest_dt is not None and latest_usable:
                    try:
                        now = pd.Timestamp.now().tz_localize(None)
                        wall_lag = float((now - latest_dt).total_seconds())
                        realtime_lag = None if realtime_dt is None else float((now - realtime_dt).total_seconds())
                        if wall_lag >= float(wall_lag_sec) and (realtime_dt is None or realtime_lag <= float(max(wall_lag_sec, 300))):
                            force_latest = True
                            reason = "wall_lag_latest_usable"
                    except Exception:
                        pass

                if force_latest:
                    payload = _merge_latest_over_history(cc, interval_i, hist, latest, normalize_fn)
                    logger.warning(
                        "[SUMMARY LATEST PREFER PATCH] force latest interval=%s reason=%s hist_dt=%s latest_dt=%s realtime_dt=%s delta_sec=%s hist_symbols=%s latest_symbols=%s rows=%s payload_latest_dt=%s lag_sec=%s",
                        interval_i,
                        reason,
                        hist_dt,
                        latest_dt,
                        realtime_dt,
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
            "[SUMMARY LATEST PREFER PATCH] installed version=%s intervals=%s lag_sec=%s wall_lag_sec=%s min_symbols=%s",
            VERSION,
            os.environ.get("SUMMARY_FORCE_LATEST_INTERVALS", "1,3,5"),
            _env_int("SUMMARY_FORCE_LATEST_WHEN_HISTORY_LAG_SEC", 120),
            _env_int("SUMMARY_FORCE_LATEST_WALL_LAG_SEC", 180),
            _env_int("SUMMARY_FORCE_LATEST_MIN_SYMBOLS", 20),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY LATEST PREFER PATCH] install failed")
        return False


__all__ = ["VERSION", "install"]
