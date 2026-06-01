# ============================================================
# File   : core/startup/tonosama_resample_mtf_from_1m_patch.py
# Version: v1-TONOSAMA-RESAMPLE-MTF-FROM-FRESH-1M
# ------------------------------------------------------------
# Purpose:
#   TONOSAMA uses 1m/3m/5m PUSH summaries.  In production logs the
#   1m summary was fresh, but 3m/5m merged summaries were still old
#   Yahoo recovery rows around 11:30.  Then volume_surge.add_volume_surge_features
#   filtered 3m/5m to zero rows and downstream guards reported
#   TONOSAMA_*_NO_STRONG_3M5M_*.
#
#   This patch wraps volume_surge.build_scalping_feature_df.  When fresh 1m
#   exists but 3m/5m are stale/empty, it resamples 3m/5m from fresh 1m rows
#   and computes the same surge/streak columns before merging.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_BUILD = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        x = float(s)
        if x != x:
            return default
        return x
    except Exception:
        return default


def _now_naive() -> dt.datetime:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive().replace(tzinfo=None)
    except Exception:
        return dt.datetime.now()


def _market_age_minutes(latest: Any, now: dt.datetime) -> float | None:
    try:
        import trading.entry.tonosama.volume_surge as vs
        fn = getattr(vs, "_market_age_minutes", None)
        if callable(fn):
            return fn(latest, now)
    except Exception:
        pass
    try:
        if latest is None or pd.isna(latest):
            return None
        return max(0.0, (now - pd.Timestamp(latest).to_pydatetime().replace(tzinfo=None)).total_seconds() / 60.0)
    except Exception:
        return None


def _latest(df: pd.DataFrame) -> pd.Timestamp | None:
    try:
        if df is None or df.empty or "datetime" not in df.columns:
            return None
        s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
        if s.empty:
            return None
        return pd.Timestamp(s.max())
    except Exception:
        return None


def _fresh_enough(df: pd.DataFrame, *, max_age_min: float) -> bool:
    try:
        latest = _latest(df)
        age = _market_age_minutes(latest, _now_naive())
        return age is not None and age <= max_age_min
    except Exception:
        return False


def _normalize_1m(raw1: pd.DataFrame) -> pd.DataFrame:
    try:
        import trading.entry.tonosama.volume_surge as vs
        return vs._filter_recent_rows(vs.normalize_summary_base(raw1, interval=1), interval=1, label="resample_base_1m")
    except Exception:
        logger.debug("[TONOSAMA RESAMPLE MTF] normalize 1m failed", exc_info=True)
        return pd.DataFrame()


def _resample_from_1m(df1_recent: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        import trading.entry.tonosama.volume_surge as vs
        if df1_recent is None or df1_recent.empty:
            return pd.DataFrame()
        x = df1_recent.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["datetime", "symbol"])
        if x.empty:
            return pd.DataFrame()
        for c in ("open", "high", "low", "close", "volume"):
            if c not in x.columns:
                if c == "open" and "open_price" in x.columns:
                    x[c] = pd.to_numeric(x["open_price"], errors="coerce")
                elif c == "high" and "high_price" in x.columns:
                    x[c] = pd.to_numeric(x["high_price"], errors="coerce")
                elif c == "low" and "low_price" in x.columns:
                    x[c] = pd.to_numeric(x["low_price"], errors="coerce")
                elif c == "close" and "close_price" in x.columns:
                    x[c] = pd.to_numeric(x["close_price"], errors="coerce")
                else:
                    x[c] = 0.0
            x[c] = pd.to_numeric(x[c], errors="coerce")

        frames: list[pd.DataFrame] = []
        rule = f"{int(interval)}min"
        for sym, g in x.sort_values("datetime").groupby("symbol", sort=False):
            gg = g.set_index("datetime")
            agg = gg.resample(rule, label="right", closed="right").agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            ).dropna(subset=["close"])
            if agg.empty:
                continue
            agg = agg.reset_index()
            agg["symbol"] = sym
            if "symbolname" in g.columns:
                try:
                    agg["symbolname"] = str(g["symbolname"].dropna().iloc[-1])
                except Exception:
                    agg["symbolname"] = ""
            else:
                agg["symbolname"] = ""
            agg["source"] = f"tonosama_resample_from_1m_{interval}m"
            frames.append(agg)
        if not frames:
            return pd.DataFrame()
        resampled = pd.concat(frames, ignore_index=True, sort=False)
        features = vs.add_volume_surge_features(resampled, interval=interval)
        if not features.empty:
            logger.warning(
                "[TONOSAMA RESAMPLE MTF] built interval=%s rows=%s symbols=%s latest=%s from_1m_rows=%s",
                interval,
                len(features),
                features["symbol"].nunique() if "symbol" in features.columns else 0,
                _latest(resampled),
                len(df1_recent),
            )
        return features
    except Exception:
        logger.exception("[TONOSAMA RESAMPLE MTF] resample failed interval=%s", interval)
        return pd.DataFrame()


def _patched_build_scalping_feature_df() -> pd.DataFrame:
    if not _env_bool("TONOSAMA_RESAMPLE_MTF_FROM_1M", True):
        return _ORIGINAL_BUILD()
    try:
        import trading.entry.tonosama.volume_surge as vs
        started = time.perf_counter()
        raw1 = vs.normalize_summary_base(vs.load_merged_summary(1), interval=1)
        raw1_info = vs._latest_info(raw1)
        df1 = vs._filter_recent_rows(raw1, interval=1, label="base_1m")
        if df1.empty:
            logger.warning(
                "[TONOSAMA SURGE] base 1m recent empty -> skip TONOSAMA for safety raw_rows=%s latest_dt=%s age_min=%s market_age_min=%s max_age_min=%.1f source_counts=%s hint=%s",
                len(raw1) if isinstance(raw1, pd.DataFrame) else 0,
                raw1_info.get("latest_dt"),
                raw1_info.get("age_min"),
                raw1_info.get("market_age_min"),
                _env_float("TONOSAMA_RECENT_MAX_AGE_MIN", 30.0),
                raw1_info.get("source_counts"),
                "summary_1m_is_stale_or_not_updating; check push summary / lunch-yahoo / main_database freshness",
            )
            return pd.DataFrame()

        max_age = max(1.0, _env_float("TONOSAMA_RECENT_MAX_AGE_MIN", 30.0))
        raw3 = vs.load_merged_summary(3)
        raw5 = vs.load_merged_summary(5)
        df3 = vs.add_volume_surge_features(raw3, interval=3)
        df5 = vs.add_volume_surge_features(raw5, interval=5)

        df1_recent = _normalize_1m(raw1)
        if (df3 is None or df3.empty or not _fresh_enough(raw3, max_age_min=max_age)) and not df1_recent.empty:
            df3_resampled = _resample_from_1m(df1_recent, 3)
            if not df3_resampled.empty:
                logger.warning("[TONOSAMA RESAMPLE MTF] replace stale/empty 3m with resampled rows=%s", len(df3_resampled))
                df3 = df3_resampled
        if (df5 is None or df5.empty or not _fresh_enough(raw5, max_age_min=max_age)) and not df1_recent.empty:
            df5_resampled = _resample_from_1m(df1_recent, 5)
            if not df5_resampled.empty:
                logger.warning("[TONOSAMA RESAMPLE MTF] replace stale/empty 5m with resampled rows=%s", len(df5_resampled))
                df5 = df5_resampled

        missing_history = vs._all_surge_history_missing(df3, df5)
        if missing_history and not vs._force_failopen_enabled():
            logger.warning("[TONOSAMA SURGE] no usable 3m/5m volume surge history after resample -> return empty base_rows=%s df3=%s df5=%s failopen_reason=%s raw1_latest=%s raw1_market_age_min=%s", len(df1), len(df3) if isinstance(df3, pd.DataFrame) else 0, len(df5) if isinstance(df5, pd.DataFrame) else 0, vs._failopen_reason(), raw1_info.get("latest_dt"), raw1_info.get("market_age_min"))
            return pd.DataFrame()
        if missing_history and vs._force_failopen_enabled():
            logger.warning("[TONOSAMA SURGE] no usable 3m/5m volume surge history after resample -> continue controlled fail-open base_rows=%s df3=%s df5=%s reason=%s raw1_latest=%s raw1_market_age_min=%s", len(df1), len(df3) if isinstance(df3, pd.DataFrame) else 0, len(df5) if isinstance(df5, pd.DataFrame) else 0, vs._failopen_reason(), raw1_info.get("latest_dt"), raw1_info.get("market_age_min"))

        out = df1.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"]).groupby("symbol", group_keys=False).tail(1).copy()
        if out.empty:
            return pd.DataFrame()
        if isinstance(df3, pd.DataFrame) and not df3.empty:
            out = out.merge(df3, on="symbol", how="left")
        if isinstance(df5, pd.DataFrame) and not df5.empty:
            out = out.merge(df5, on="symbol", how="left")

        for c in ["volume_surge_ratio_3m", "volume_surge_ratio_5m", "price_change_pct_3m", "price_change_pct_5m", "prev5_volume_avg_3m", "prev5_volume_avg_5m", "volume_3m", "volume_5m", "prev_3m_up_streak", "prev_3m_down_streak", "prev_5m_up_streak", "prev_5m_down_streak", "prev_3m_last_delta_pct", "prev_5m_last_delta_pct"]:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        out = vs._apply_history_unavailable_policy(out)
        if out.empty:
            return pd.DataFrame()

        vol_cols = [c for c in ["volume_surge_ratio_3m", "volume_surge_ratio_5m"] if c in out.columns]
        price_cols = [c for c in ["price_change_pct_3m", "price_change_pct_5m"] if c in out.columns]
        out["_max_volume_surge_ratio"] = out[vol_cols].max(axis=1, skipna=True) if vol_cols else 0.0
        out["_max_price_change_pct"] = out[price_cols].max(axis=1, skipna=True) if price_cols else 0.0
        out["_max_volume_surge_ratio"] = pd.to_numeric(out["_max_volume_surge_ratio"], errors="coerce").fillna(0.0)
        out["_max_price_change_pct"] = pd.to_numeric(out["_max_price_change_pct"], errors="coerce").fillna(0.0)
        for c in ["prev_3m_up_streak", "prev_3m_down_streak", "prev_5m_up_streak", "prev_5m_down_streak"]:
            if c not in out.columns:
                out[c] = 0
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).astype(int)
        for c in ["prev_3m_last_delta_pct", "prev_5m_last_delta_pct"]:
            if c not in out.columns:
                out[c] = 0.0
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
        out["_surge_tf"] = ""
        if "volume_surge_ratio_3m" in out.columns and "volume_surge_ratio_5m" in out.columns:
            out["_surge_tf"] = out.apply(lambda r: "3m" if _sf(r.get("volume_surge_ratio_3m"), 0) >= _sf(r.get("volume_surge_ratio_5m"), 0) else "5m", axis=1)
        elif "volume_surge_ratio_3m" in out.columns:
            out["_surge_tf"] = "3m"
        elif "volume_surge_ratio_5m" in out.columns:
            out["_surge_tf"] = "5m"

        try:
            history_missing = out.get("_volume_surge_history_missing", pd.Series(False, index=out.index)).fillna(False).astype(bool)
            failopen_col = out.get("_volume_surge_failopen", pd.Series(False, index=out.index)).fillna(False).astype(bool)
            up3 = out.get("prev_3m_up_streak", pd.Series(0, index=out.index)).fillna(0)
            up5 = out.get("prev_5m_up_streak", pd.Series(0, index=out.index)).fillna(0)
            dn3 = out.get("prev_3m_down_streak", pd.Series(0, index=out.index)).fillna(0)
            dn5 = out.get("prev_5m_down_streak", pd.Series(0, index=out.index)).fillna(0)
            logger.warning(
                "[TONOSAMA RESAMPLE MTF] feature summary rows=%s vol_cols=%s price_cols=%s volume_surge_nonzero=%s price_change_nonzero=%s up_3m_or_5m_ge3=%s down_3m_or_5m_ge3=%s history_missing_rows=%s failopen_rows=%s raw1_latest=%s raw1_market_age_min=%s elapsed=%.3fs head=%s",
                len(out),
                vol_cols,
                price_cols,
                int((out["_max_volume_surge_ratio"].fillna(0) != 0).sum()),
                int((out["_max_price_change_pct"].fillna(0) != 0).sum()),
                int(((up3 >= 3) | (up5 >= 3)).sum()),
                int(((dn3 >= 3) | (dn5 >= 3)).sum()),
                int(history_missing.sum()),
                int(failopen_col.sum()),
                raw1_info.get("latest_dt"),
                raw1_info.get("market_age_min"),
                time.perf_counter() - started,
                out[[c for c in ["symbol", "symbolname", "close", "_max_volume_surge_ratio", "_max_price_change_pct", "prev_3m_up_streak", "prev_5m_up_streak", "prev_3m_down_streak", "prev_5m_down_streak", "prev_3m_last_delta_pct", "prev_5m_last_delta_pct", "_surge_tf", "_volume_surge_history_missing", "_volume_surge_failopen"] if c in out.columns]].head(12).to_dict("records"),
            )
        except Exception:
            logger.debug("[TONOSAMA RESAMPLE MTF] feature summary log failed", exc_info=True)
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[TONOSAMA RESAMPLE MTF] patched build failed -> original")
        return _ORIGINAL_BUILD()


def install() -> bool:
    global _PATCHED, _ORIGINAL_BUILD
    if _PATCHED:
        return True
    try:
        import trading.entry.tonosama.volume_surge as vs
        cur = getattr(vs, "build_scalping_feature_df", None)
        if not callable(cur):
            logger.warning("[TONOSAMA RESAMPLE MTF] target missing")
            return False
        if getattr(cur, "_tonosama_resample_mtf_from_1m_v1", False):
            _PATCHED = True
            return True
        _ORIGINAL_BUILD = getattr(cur, "_original", cur)
        _patched_build_scalping_feature_df._tonosama_resample_mtf_from_1m_v1 = True  # type: ignore[attr-defined]
        _patched_build_scalping_feature_df._original = _ORIGINAL_BUILD  # type: ignore[attr-defined]
        vs.build_scalping_feature_df = _patched_build_scalping_feature_df
        _PATCHED = True
        logger.warning("[TONOSAMA RESAMPLE MTF] installed v1 enabled=%s", _env_bool("TONOSAMA_RESAMPLE_MTF_FROM_1M", True))
        return True
    except Exception:
        logger.exception("[TONOSAMA RESAMPLE MTF] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA RESAMPLE MTF] auto install failed")


__all__ = ["install"]
