# ============================================================
# File   : core/startup/summary_stale_guard_patch.py
# Version: REV8-SUMMARY-STALE-GUARD-SAME-DAY-SOFT-LAG
# ------------------------------------------------------------
# 【概要】
#   PUSH / ranking summary が古いまま merged summary に残り、
#   古い価格・古い slope・古い RSI/MACD で表示・AI・発注候補になる問題を防ぐ。
#
# REV8:
#   - 当日データの latest_dt が max_age を少し超えただけで rows=0 にする
#     global-lag hard-drop を緩和。
#   - 前日・非当日データは引き続き必ず破棄する。
#   - 当日データは absolute max_age と relative_lag の通常フィルターで残す。
#     これにより、09:23台に latest_dt=09:21 のPUSH summaryを全DROPしない。
#
# REV7:
#   - 市場中の PUSH / ranking / legacy intraday summary は、当日以外を必ず破棄。
#   - latest_dt が max_age を超えた場合、global-lag fallback で古い行を残さない。
#   - min_keep fallback の既定値を 0 に変更し、古い前日データを「最低件数維持」で残さない。
#   - 旧挙動が必要な場合のみ、環境変数で明示的に戻せる。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SANITIZE_SUMMARY_DF = None
_ORIGINAL_GET_MERGED_SUMMARY = None
VERSION = "REV8-SUMMARY-STALE-GUARD-SAME-DAY-SOFT-LAG"


def _env_bool(name: str, default: bool) -> bool:
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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _normalize_source(source: Any) -> str:
    try:
        return str(source or "push").strip().lower()
    except Exception:
        return "push"


def _normalize_tf(tf: Any) -> Any:
    try:
        if tf in ("1", "1m", "1min"):
            return 1
        if tf in ("3", "3m", "3min"):
            return 3
        if tf in ("5", "5m", "5min"):
            return 5
        if tf in ("10", "10m", "10min"):
            return 10
        if tf in ("15", "15m", "15min"):
            return 15
        if tf in ("30", "30m", "30min"):
            return 30
        if tf in ("60", "60m", "60min"):
            return 60
        if tf in ("d", "1d", "day", "daily"):
            return "daily"
        return int(tf)
    except Exception:
        return tf


def _is_intraday_live_source(source: Any) -> bool:
    src = _normalize_source(source)
    return src.startswith("push") or src.startswith("ranking") or src in {"legacy", "summary"}


def _max_age_sec(source: Any, tf: Any) -> Optional[int]:
    source = _normalize_source(source)
    tf = _normalize_tf(tf)

    if not _env_bool("SUMMARY_STALE_GUARD_ENABLED", True):
        return None

    if tf == "daily":
        return None

    if source.startswith("push"):
        defaults = {1: 120, 3: 240, 5: 420, 10: 1200, 15: 1500, 30: 2400, 60: 4800}
        default = int(defaults.get(tf, 600))
        return _env_int(f"PUSH_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

    if source.startswith("ranking"):
        defaults = {1: 180, 3: 300, 5: 480, 10: 1500, 15: 1800, 30: 3600, 60: 7200}
        default = int(defaults.get(tf, 900))
        return _env_int(f"RANKING_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

    if source == "legacy" or source == "summary":
        default = 180 if tf == 1 else 480
        return _env_int(f"LEGACY_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

    return None


def _relative_lag_sec(source: Any, tf: Any) -> Optional[int]:
    source = _normalize_source(source)
    tf = _normalize_tf(tf)

    if not _env_bool("SUMMARY_STALE_RELATIVE_GUARD_ENABLED", True):
        return None
    if tf == "daily":
        return None

    try:
        tf_int = int(tf)
    except Exception:
        tf_int = 1

    default = max(180, tf_int * 120 + 60)

    if source.startswith("push"):
        return _env_int(f"PUSH_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default)
    if source.startswith("ranking"):
        return _env_int(f"RANKING_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default)
    if source in {"legacy", "summary"}:
        return _env_int(f"LEGACY_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default)
    return None


def _min_keep_rows(source: Any, tf: Any) -> int:
    """Old default kept 50 rows even when all rows were stale.

    For live trading this is dangerous: it can keep previous-day rows and make
    downstream AI/entry logic believe there are candidates. Default is now 0.
    """
    tf_n = _normalize_tf(tf)
    if tf_n == "daily":
        return 0
    try:
        tf_int = int(tf_n)
    except Exception:
        tf_int = 1

    src = _normalize_source(source).upper()
    return _env_int(f"{src}_SUMMARY_{tf_int}MIN_KEEP_ROWS", _env_int(f"SUMMARY_{tf_int}MIN_KEEP_ROWS", 0))


def _future_allow_sec(source: Any, tf: Any) -> int:
    try:
        tf_n = _normalize_tf(tf)
        tf_int = int(tf_n) if tf_n != "daily" else 1
    except Exception:
        tf_int = 1
    default = max(10, min(60, tf_int * 12))
    src = _normalize_source(source).upper()
    return _env_int(f"{src}_SUMMARY_{tf_int}MIN_FUTURE_ALLOW_SEC", _env_int("SUMMARY_FUTURE_ALLOW_SEC", default))


def _best_time_col(df: pd.DataFrame) -> Optional[str]:
    try:
        for col in ("datetime", "end_time", "start_time", "time"):
            if col in df.columns:
                return col
        return None
    except Exception:
        return None


def _latest_fallback_rows(out: pd.DataFrame, *, time_col: str, min_keep: int) -> pd.DataFrame:
    if min_keep <= 0 or out.empty:
        return out.iloc[0:0].copy()
    try:
        work = out[out[time_col].notna()].copy()
        if work.empty:
            return out.iloc[0:0].copy()
        work = work.sort_values(time_col, ascending=False)
        if "symbol" in work.columns:
            work = work.drop_duplicates(subset=["symbol"], keep="first")
        return work.head(int(min_keep)).copy().reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY STALE DROP] latest fallback failed", exc_info=True)
        return out.iloc[0:0].copy()


def _to_naive_datetime_series(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s, errors="coerce")
    try:
        return out.dt.tz_localize(None)
    except Exception:
        return out


def _same_day_latest(latest_dt: Any, now: pd.Timestamp) -> bool:
    try:
        if latest_dt is None or pd.isna(latest_dt):
            return False
        return pd.Timestamp(latest_dt).date() == pd.Timestamp(now).date()
    except Exception:
        return False


def drop_stale_summary_rows(df: Any, *, source: Any, tf: Any, label: str = "sanitize") -> pd.DataFrame:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame() if df is None else df

        max_age = _max_age_sec(source, tf)
        relative_lag = _relative_lag_sec(source, tf)
        min_keep = _min_keep_rows(source, tf)
        if (max_age is None or max_age <= 0) and (relative_lag is None or relative_lag <= 0):
            return df

        time_col = _best_time_col(df)
        if not time_col:
            logger.warning(
                "[SUMMARY STALE DROP] source=%s tf=%s label=%s before=%s after=0 reason=time_col_missing max_age_sec=%s relative_lag_sec=%s version=%s",
                source, tf, label, len(df), max_age, relative_lag, VERSION,
            )
            return df.iloc[0:0].copy()

        out = df.copy()
        out[time_col] = _to_naive_datetime_series(out[time_col])
        now = pd.Timestamp.now().tz_localize(None)
        before_original = int(len(out))

        # Live intraday data must be today. Previous trading day rows are not safe
        # for display, AI or entry, even when they are the latest rows in a DB.
        today_str = now.strftime("%Y-%m-%d")
        strict_today = _env_bool("SUMMARY_STALE_STRICT_TODAY_ONLY", True)
        if strict_today and _is_intraday_live_source(source) and _normalize_tf(tf) != "daily":
            dt_str = out[time_col].dt.strftime("%Y-%m-%d")
            non_today_mask = out[time_col].notna() & dt_str.ne(today_str)
            non_today_count = int(non_today_mask.sum()) if len(out) else 0
            if non_today_count > 0:
                non_today_latest = None
                try:
                    non_today_latest = out.loc[non_today_mask, time_col].max()
                except Exception:
                    non_today_latest = None
                out = out.loc[~non_today_mask].copy().reset_index(drop=True)
                logger.warning(
                    "[SUMMARY NON-TODAY DROP] source=%s tf=%s label=%s before=%s non_today_removed=%s after=%s today=%s non_today_latest=%s version=%s",
                    source, tf, label, before_original, non_today_count, len(out), today_str, non_today_latest, VERSION,
                )
                if out.empty:
                    return out

        # Future rows are invalid for live trading and must never be kept.
        allow_sec = _future_allow_sec(source, tf)
        future_cutoff = now + pd.Timedelta(seconds=float(allow_sec))
        dt_series0 = out[time_col]
        future_mask = dt_series0.notna() & dt_series0.gt(future_cutoff)
        future_count = int(future_mask.sum()) if len(out) else 0
        if future_count > 0:
            future_latest = None
            try:
                future_latest = dt_series0.loc[future_mask].max()
            except Exception:
                future_latest = None
            out = out.loc[~future_mask].copy().reset_index(drop=True)
            logger.warning(
                "[SUMMARY FUTURE DROP] source=%s tf=%s label=%s before=%s future_removed=%s after=%s now=%s allow_sec=%s future_latest=%s version=%s",
                source, tf, label, before_original, future_count, len(out), now, allow_sec, future_latest, VERSION,
            )
            if out.empty:
                return out

        dt_series = out[time_col]
        age_sec = (now - dt_series).dt.total_seconds()
        before = int(len(out))
        valid_mask = dt_series.notna() & age_sec.ge(0)

        latest_dt = None
        latest_age_sec = None
        try:
            latest_dt = dt_series.max()
            if pd.notna(latest_dt):
                latest_age_sec = float((now - latest_dt).total_seconds())
        except Exception:
            latest_dt = None
            latest_age_sec = None

        # REV8: Do not hard-drop same-day rows only because the latest row is a
        # few minutes behind. Rotation/rebuild can lag briefly. Previous-day rows
        # are already removed above, so same-day data can safely pass through the
        # normal absolute/relative masks instead of becoming rows=0.
        global_lag_enabled = _env_bool("SUMMARY_STALE_KEEP_LATEST_PER_SYMBOL_ON_GLOBAL_LAG", False)
        global_lag_grace = _env_int("SUMMARY_STALE_GLOBAL_LAG_GRACE_SEC", 30)
        hard_drop_same_day = _env_bool("SUMMARY_STALE_HARD_DROP_SAME_DAY_GLOBAL_LAG", False)
        latest_is_today = _same_day_latest(latest_dt, now)
        if (
            not global_lag_enabled
            and max_age is not None
            and max_age > 0
            and latest_age_sec is not None
            and latest_age_sec > float(max_age + global_lag_grace)
        ):
            if latest_is_today and not hard_drop_same_day:
                logger.warning(
                    "[SUMMARY STALE DROP] global lag soft-keep same-day source=%s tf=%s label=%s before=%s max_age_sec=%s latest_age_sec=%.1f latest_dt=%s now=%s version=%s",
                    source, tf, label, before, max_age, latest_age_sec, latest_dt, now, VERSION,
                )
            else:
                logger.warning(
                    "[SUMMARY STALE DROP] global lag hard-drop source=%s tf=%s label=%s before=%s after=0 max_age_sec=%s latest_age_sec=%.1f latest_dt=%s now=%s version=%s",
                    source, tf, label, before, max_age, latest_age_sec, latest_dt, now, VERSION,
                )
                return out.iloc[0:0].copy()

        absolute_mask = pd.Series(False, index=out.index)
        if max_age is not None and max_age > 0:
            absolute_mask = age_sec.le(float(max_age))

        relative_mask = pd.Series(False, index=out.index)
        if relative_lag is not None and relative_lag > 0:
            try:
                if pd.notna(latest_dt):
                    cutoff = latest_dt - pd.Timedelta(seconds=float(relative_lag))
                    relative_mask = dt_series.ge(cutoff)
            except Exception:
                latest_dt = None

        valid_mask = valid_mask & (absolute_mask | relative_mask)
        out2 = out.loc[valid_mask].copy().reset_index(drop=True)
        after = int(len(out2))

        if min_keep > 0 and before >= min_keep and after < min_keep:
            fallback = _latest_fallback_rows(out, time_col=time_col, min_keep=min_keep)
            if len(fallback) > after:
                logger.warning(
                    "[SUMMARY STALE DROP] min_keep fallback source=%s tf=%s label=%s before=%s stale_after=%s fallback_after=%s min_keep=%s latest_dt=%s now=%s version=%s",
                    source, tf, label, before, after, len(fallback), min_keep, latest_dt, now, VERSION,
                )
                out2 = fallback
                after = int(len(out2))

        if after != before or future_count > 0 or before_original != before:
            oldest_kept = None
            newest_kept = None
            try:
                oldest_kept = out2[time_col].min() if after else None
                newest_kept = out2[time_col].max() if after else None
            except Exception:
                pass

            logger.warning(
                "[SUMMARY STALE DROP] source=%s tf=%s label=%s before=%s after=%s max_age_sec=%s relative_lag_sec=%s min_keep=%s latest_dt=%s oldest_kept=%s newest_kept=%s now=%s future_removed=%s version=%s",
                source, tf, label, before_original, after, max_age, relative_lag, min_keep, latest_dt, oldest_kept, newest_kept, now, future_count, VERSION,
            )

        return out2
    except Exception:
        logger.exception("[SUMMARY STALE DROP] failed source=%s tf=%s label=%s", source, tf, label)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _install_active_empty_universe_patch() -> None:
    try:
        if os.getenv("DISABLE_ACTIVE_EMPTY_UNIVERSE_PATCH", "").strip() == "1":
            logger.warning("[SUMMARY STALE GUARD PATCH] active empty-universe patch disabled by env")
            return
        from core.startup.active_symbols_empty_universe_supplement_patch import install as install_active_patch
        ok = bool(install_active_patch())
        logger.warning("[SUMMARY STALE GUARD PATCH] active empty-universe patch ok=%s", ok)
    except Exception:
        logger.exception("[SUMMARY STALE GUARD PATCH] active empty-universe patch install failed")


def install() -> bool:
    global _PATCHED, _ORIGINAL_SANITIZE_SUMMARY_DF, _ORIGINAL_GET_MERGED_SUMMARY

    if _PATCHED:
        _install_active_empty_universe_patch()
        return True

    try:
        import core.global_context.context as ctx

        original_sanitize = getattr(ctx, "_sanitize_summary_df", None)
        if original_sanitize is None:
            logger.warning("[SUMMARY STALE GUARD PATCH] skipped: _sanitize_summary_df not found")
            _install_active_empty_universe_patch()
            return False
        _ORIGINAL_SANITIZE_SUMMARY_DF = original_sanitize

        def _sanitize_summary_df_with_stale_guard(df: Any, tf: Any, source: str, symbol_name_map=None) -> pd.DataFrame:
            out = original_sanitize(df, tf=tf, source=source, symbol_name_map=symbol_name_map)
            return drop_stale_summary_rows(out, source=source, tf=tf, label="merged_sanitize")

        ctx._sanitize_summary_df = _sanitize_summary_df_with_stale_guard

        global_context_cls = getattr(ctx, "GlobalContext", None)
        original_get_merged = getattr(global_context_cls, "get_merged_summary", None) if global_context_cls is not None else None
        if original_get_merged is not None:
            _ORIGINAL_GET_MERGED_SUMMARY = original_get_merged

            def _get_merged_summary_with_stale_guard(self, tf: Any, source: Optional[str] = None) -> pd.DataFrame:
                df = original_get_merged(self, tf=tf, source=source)
                src = source if source is not None else "push"
                return drop_stale_summary_rows(df, source=src, tf=tf, label="merged_get")

            setattr(global_context_cls, "get_merged_summary", _get_merged_summary_with_stale_guard)

        _PATCHED = True
        _install_active_empty_universe_patch()
        logger.info("[SUMMARY STALE GUARD PATCH] installed version=%s sanitize=True merged_get=%s active_empty_universe=True", VERSION, bool(original_get_merged))
        return True

    except Exception:
        logger.exception("[SUMMARY STALE GUARD PATCH] install failed")
        _install_active_empty_universe_patch()
        return False


__all__ = ["install", "drop_stale_summary_rows", "VERSION"]
