# ============================================================
# File   : core/startup/summary_stale_guard_patch.py
# Version: REV1-SUMMARY-STALE-GUARD
# ------------------------------------------------------------
# 【概要】
#   PUSH / ranking summary が古いまま merged summary に残り、
#   古い価格・古い slope・古い RSI/MACD でエントリー候補になる問題を防ぐ。
#
# 【方針】
#   - core.global_context.context の sanitize を monkey patch する。
#   - 表示用 / エントリー候補用 merged summary だけを stale 除外する。
#   - 計算用 summary_history は履歴が必要なので除外しない。
#   - 既存の context.py を大きく改変せず、安全に起動時 install する。
#
# 【主なログ】
#   [SUMMARY STALE DROP] source=push tf=1 before=23 after=0 max_age_sec=120 latest_dt=...
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SANITIZE_SUMMARY_DF = None


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
    source/tf 別の stale 秒数。

    settings.ini から直接読む構成ではないため、起動前に環境変数で上書き可能にする。
    例:
      SUMMARY_STALE_GUARD_ENABLED=1
      PUSH_SUMMARY_1MIN_MAX_AGE_SEC=120
      PUSH_SUMMARY_3MIN_MAX_AGE_SEC=240
      PUSH_SUMMARY_5MIN_MAX_AGE_SEC=420
    """
    source = _normalize_source(source)
    tf = _normalize_tf(tf)

    if not _env_bool("SUMMARY_STALE_GUARD_ENABLED", True):
        return None

    # daily は履歴性が強いため stale 除外しない。
    if tf == "daily":
        return None

    # push-cache / push-legacy-attr も PUSH 扱い。
    if source.startswith("push"):
        defaults = {
            1: 120,
            3: 240,
            5: 420,
            10: 720,
            15: 900,
            30: 1800,
            60: 3600,
        }
        default = int(defaults.get(tf, 300))
        return _env_int(f"PUSH_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

    if source == "ranking":
        defaults = {
            1: 180,
            3: 300,
            5: 480,
            10: 900,
            15: 1200,
            30: 2400,
            60: 4800,
        }
        default = int(defaults.get(tf, 600))
        return _env_int(f"RANKING_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

    # legacy は push 由来の可能性があるため、短すぎない値で除外する。
    if source == "legacy":
        default = 300 if tf == 1 else 600
        return _env_int(f"LEGACY_SUMMARY_{tf}MIN_MAX_AGE_SEC", default)

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
        if max_age is None or max_age <= 0:
            return df

        time_col = _best_time_col(df)
        if not time_col:
            logger.warning(
                "[SUMMARY STALE DROP] source=%s tf=%s label=%s before=%s after=0 reason=time_col_missing max_age_sec=%s",
                source,
                tf,
                label,
                len(df),
                max_age,
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
        valid_mask = dt_series.notna() & age_sec.ge(0) & age_sec.le(float(max_age))
        out2 = out.loc[valid_mask].copy().reset_index(drop=True)
        after = int(len(out2))

        if after != before:
            latest_dt = None
            oldest_kept = None
            try:
                latest_dt = dt_series.max()
            except Exception:
                latest_dt = None
            try:
                oldest_kept = out2[time_col].min() if after else None
            except Exception:
                oldest_kept = None

            logger.warning(
                "[SUMMARY STALE DROP] source=%s tf=%s label=%s before=%s after=%s max_age_sec=%s latest_dt=%s oldest_kept=%s now=%s",
                source,
                tf,
                label,
                before,
                after,
                max_age,
                latest_dt,
                oldest_kept,
                now,
            )

        return out2
    except Exception:
        logger.exception("[SUMMARY STALE DROP] failed source=%s tf=%s label=%s", source, tf, label)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def install() -> bool:
    global _PATCHED, _ORIGINAL_SANITIZE_SUMMARY_DF

    if _PATCHED:
        return True

    try:
        import core.global_context.context as ctx

        original = getattr(ctx, "_sanitize_summary_df", None)
        if original is None:
            logger.warning("[SUMMARY STALE GUARD PATCH] skipped: _sanitize_summary_df not found")
            return False

        _ORIGINAL_SANITIZE_SUMMARY_DF = original

        def _sanitize_summary_df_with_stale_guard(
            df: Any,
            tf: Any,
            source: str,
            symbol_name_map=None,
        ) -> pd.DataFrame:
            out = original(df, tf=tf, source=source, symbol_name_map=symbol_name_map)
            return drop_stale_summary_rows(out, source=source, tf=tf, label="merged_sanitize")

        ctx._sanitize_summary_df = _sanitize_summary_df_with_stale_guard

        _PATCHED = True
        logger.info("[SUMMARY STALE GUARD PATCH] installed")
        return True

    except Exception:
        logger.exception("[SUMMARY STALE GUARD PATCH] install failed")
        return False


__all__ = ["install", "drop_stale_summary_rows"]
