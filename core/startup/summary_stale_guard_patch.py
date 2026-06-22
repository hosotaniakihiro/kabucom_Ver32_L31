# ============================================================
# File   : core/startup/summary_stale_guard_patch.py
# Version: REV3-SUMMARY-STALE-GUARD-MERGED-GET-ACTIVE-EMPTY-UNIVERSE
# ------------------------------------------------------------
# 【概要】
#   PUSH / ranking summary が古いまま merged summary に残り、
#   古い価格・古い slope・古い RSI/MACD でエントリー候補になる問題を防ぐ。
#
# 【方針】
#   - core.global_context.context の sanitize / get_merged_summary を monkey patch する。
#   - 表示用 / エントリー候補用 merged summary だけを stale 除外する。
#   - 計算用 summary_history は履歴が必要なので除外しない。
#   - 最新足が 3m/5m の完成足として少し遅れるケースを考慮し、
#     「現在時刻からの絶対 stale」だけでなく「最新足から見て古すぎる行」も落とす。
#   - DB系プロセスでランキング universe が空のとき、active symbols 補充が全落ちしない
#     軽量パッチも同時に入れる。
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


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
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


def _max_age_sec(source: Any, tf: Any) -> Optional[int]:
    """
    source/tf 別の絶対 stale 秒数。

    注意:
      3m/5m は最新完成足が現在時刻より数分前になるため、
      drop_stale_summary_rows() では絶対 stale に加え、最新足からの相対 stale も使う。
    """
    source = _normalize_source(source)
    tf = _normalize_tf(tf)

    if not _env_bool("SUMMARY_STALE_GUARD_ENABLED", True):
        return None

    if tf == "daily":
        return None

    if source.startswith("push"):
        defaults = {
            1: 180,
            3: 600,
            5: 900,
            10: 1200,
            15: 1500,
            30: 2400,
            60: 4800,
        }
        default = int(defaults.get(tf, 600))
        return _env_int(f"PUSH_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

    if source == "ranking":
        defaults = {
            1: 240,
            3: 720,
            5: 1020,
            10: 1500,
            15: 1800,
            30: 3600,
            60: 7200,
        }
        default = int(defaults.get(tf, 900))
        return _env_int(f"RANKING_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

    if source == "legacy":
        default = 300 if tf == 1 else 900
        return _env_int(f"LEGACY_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

    return None


def _relative_lag_sec(source: Any, tf: Any) -> Optional[int]:
    """最新足から見て、銘柄行として許容する遅れ秒数。"""
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

    # 例: 3m は最新足から約7分、5m は約11分まで許容。
    # 13:04 時点で最新3m足が 13:00 のようなケースは残しつつ、10:15などを落とす。
    default = max(180, tf_int * 120 + 60)

    if source.startswith("push"):
        return _env_int(f"PUSH_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default)
    if source == "ranking":
        return _env_int(f"RANKING_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default)
    if source == "legacy":
        return _env_int(f"LEGACY_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default)
    return None


def _best_time_col(df: pd.DataFrame) -> Optional[str]:
    try:
        for col in ("datetime", "end_time", "start_time", "time"):
            if col in df.columns:
                return col
        return None
    except Exception:
        return None


def drop_stale_summary_rows(df: Any, *, source: Any, tf: Any, label: str = "sanitize") -> pd.DataFrame:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame() if df is None else df

        max_age = _max_age_sec(source, tf)
        relative_lag = _relative_lag_sec(source, tf)
        if (max_age is None or max_age <= 0) and (relative_lag is None or relative_lag <= 0):
            return df

        time_col = _best_time_col(df)
        if not time_col:
            logger.warning(
                "[SUMMARY STALE DROP] source=%s tf=%s label=%s before=%s after=0 reason=time_col_missing max_age_sec=%s relative_lag_sec=%s",
                source,
                tf,
                label,
                len(df),
                max_age,
                relative_lag,
            )
            return df.iloc[0:0].copy()

        out = df.copy()
        out[time_col] = pd.to_datetime(out[time_col], errors="coerce")
        try:
            out[time_col] = out[time_col].dt.tz_localize(None)
        except Exception:
            pass

        now = pd.Timestamp.now()
        dt_series = out[time_col]
        age_sec = (now - dt_series).dt.total_seconds()

        before = int(len(out))
        valid_mask = dt_series.notna() & age_sec.ge(0)

        absolute_mask = pd.Series(False, index=out.index)
        if max_age is not None and max_age > 0:
            absolute_mask = age_sec.le(float(max_age))

        relative_mask = pd.Series(False, index=out.index)
        latest_dt = None
        if relative_lag is not None and relative_lag > 0:
            try:
                latest_dt = dt_series.max()
                if pd.notna(latest_dt):
                    cutoff = latest_dt - pd.Timedelta(seconds=float(relative_lag))
                    relative_mask = dt_series.ge(cutoff)
            except Exception:
                latest_dt = None

        # 絶対 stale または最新足からの相対 stale のどちらかを満たせば残す。
        # 3m/5m の最新完成足が数分前でも、最新足周辺の銘柄は落とさない。
        valid_mask = valid_mask & (absolute_mask | relative_mask)

        out2 = out.loc[valid_mask].copy().reset_index(drop=True)
        after = int(len(out2))

        if after != before:
            oldest_kept = None
            newest_kept = None
            try:
                oldest_kept = out2[time_col].min() if after else None
                newest_kept = out2[time_col].max() if after else None
            except Exception:
                pass

            logger.warning(
                "[SUMMARY STALE DROP] source=%s tf=%s label=%s before=%s after=%s max_age_sec=%s relative_lag_sec=%s latest_dt=%s oldest_kept=%s newest_kept=%s now=%s",
                source,
                tf,
                label,
                before,
                after,
                max_age,
                relative_lag,
                latest_dt,
                oldest_kept,
                newest_kept,
                now,
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

        def _sanitize_summary_df_with_stale_guard(
            df: Any,
            tf: Any,
            source: str,
            symbol_name_map=None,
        ) -> pd.DataFrame:
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
        logger.info("[SUMMARY STALE GUARD PATCH] installed rev=3 sanitize=True merged_get=%s active_empty_universe=True", bool(original_get_merged))
        return True

    except Exception:
        logger.exception("[SUMMARY STALE GUARD PATCH] install failed")
        _install_active_empty_universe_patch()
        return False


__all__ = ["install", "drop_stale_summary_rows"]
