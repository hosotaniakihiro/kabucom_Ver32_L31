# ============================================================
# File   : core/startup/summary_seed_recent_merged_guard_patch.py
# Version: V1-SUMMARY-SEED-RECENT-MERGED-GUARD
# ------------------------------------------------------------
# 目的:
#   summary_db_seed_restore_patch は history cache へは全量復元してよいが、
#   source=push の merged latest へは当日・直近行だけを載せる。
#
# 背景:
#   当日 summaryYYYYMMDD.db の中に 2026-05-25 / 2026-05-26 の行が混在していると、
#   最新dtが当日というだけで、古い行もまとめて PUSH merged に投入されていた。
#   その結果、5MA判定・殿様・SUMMARY AI・表示が古い足を参照する可能性がある。
#
# 方針:
#   - GC.set_summary_history(interval, df, source="db_seed") は全量維持
#   - global_data.summary_Xm_df も全量維持
#   - set_push_merged_summary / set_ranking_merged_summary 直前だけ当日直近に絞る
#
# ENV:
#   SUMMARY_SEED_RECENT_MERGED_GUARD_ENABLED=1
#   SUMMARY_SEED_MERGED_MAX_AGE_MIN=240
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_PUBLISH = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return max(1.0, float(raw))
    except Exception:
        return float(default)


def _now_naive() -> dt.datetime:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive().replace(tzinfo=None)
    except Exception:
        return dt.datetime.now()


def _nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
    except Exception:
        return -1


def _filter_recent_for_merged(df: pd.DataFrame, *, interval: int, source_name: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if "datetime" not in df.columns:
        return df.copy()
    try:
        out = df.copy()
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        before = len(out)
        now = _now_naive()
        today = pd.Timestamp(now.date())
        cutoff = pd.Timestamp(now - dt.timedelta(minutes=_env_float("SUMMARY_SEED_MERGED_MAX_AGE_MIN", 240.0)))
        out = out[(out["datetime"].notna()) & (out["datetime"] >= today) & (out["datetime"] >= cutoff)].copy()
        dropped = before - len(out)
        if dropped > 0:
            try:
                min_dt = pd.to_datetime(df["datetime"], errors="coerce").min()
                max_dt = pd.to_datetime(df["datetime"], errors="coerce").max()
            except Exception:
                min_dt = max_dt = None
            logger.warning(
                "[SUMMARY SEED MERGED GUARD] stale seed rows filtered interval=%s source=%s before=%s after=%s dropped=%s cutoff=%s today=%s original_min_dt=%s original_max_dt=%s",
                interval,
                source_name,
                before,
                len(out),
                dropped,
                cutoff,
                today.date(),
                min_dt,
                max_dt,
            )
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY SEED MERGED GUARD] filter failed interval=%s source=%s", interval, source_name)
        return df.copy()


def _patched_publish_interval(interval: int, df: pd.DataFrame) -> dict[str, Any]:
    stats: dict[str, Any] = {"interval": interval, "rows": 0, "push_rows": 0, "ranking_rows": 0}
    if df is None or df.empty:
        return stats

    try:
        from global_state import global_data
        from core.global_context.context import global_context as GC
        import core.startup.summary_db_seed_restore_patch as seed_mod

        try:
            GC.set_summary_history(interval, df, source="db_seed")
        except Exception:
            logger.exception("[SUMMARY SEED MERGED GUARD] set_summary_history failed interval=%s", interval)

        try:
            setattr(global_data, f"summary_{interval}m_df", df.copy())
        except Exception:
            pass

        stale_for_merged, stale_reason = seed_mod._seed_is_stale_for_merged(df)
        push_df = pd.DataFrame()
        ranking_df = pd.DataFrame()
        if stale_for_merged:
            logger.warning(
                "[SUMMARY SEED MERGED GUARD] history-only seed interval=%s reason=%s rows=%s; skip merged",
                interval,
                stale_reason,
                len(df),
            )
        else:
            src = df["source"].fillna("push").astype(str).str.lower() if "source" in df.columns else pd.Series("push", index=df.index)
            ranking_mask = src.str.contains("ranking|rank", regex=True, na=False)
            push_df_raw = df.loc[~ranking_mask].copy()
            ranking_df_raw = df.loc[ranking_mask].copy()
            push_df = _filter_recent_for_merged(push_df_raw, interval=int(interval), source_name="push")
            ranking_df = _filter_recent_for_merged(ranking_df_raw, interval=int(interval), source_name="ranking")

            if not push_df.empty:
                try:
                    global_data.set_push_merged_summary(interval, push_df)
                except Exception:
                    logger.exception("[SUMMARY SEED MERGED GUARD] set_push_merged_summary failed interval=%s", interval)
            elif not push_df_raw.empty:
                logger.warning(
                    "[SUMMARY SEED MERGED GUARD] skip push merged because recent rows empty interval=%s raw_rows=%s",
                    interval,
                    len(push_df_raw),
                )

            if not ranking_df.empty:
                try:
                    global_data.set_ranking_merged_summary(interval, ranking_df)
                except Exception:
                    logger.exception("[SUMMARY SEED MERGED GUARD] set_ranking_merged_summary failed interval=%s", interval)

        stats.update(
            rows=int(len(df)),
            push_rows=int(len(push_df)),
            ranking_rows=int(len(ranking_df)),
            merged_skipped_stale=int(bool(stale_for_merged)),
            merged_skip_reason=stale_reason,
            symbols=int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
            latest_dt=str(pd.to_datetime(df["datetime"], errors="coerce").max()) if "datetime" in df.columns else None,
            min_dt=str(pd.to_datetime(df["datetime"], errors="coerce").min()) if "datetime" in df.columns else None,
            max_rows_per_symbol=int(df.groupby("symbol").size().max()) if "symbol" in df.columns and len(df) else 0,
            macd_nonzero=int((pd.to_numeric(df.get("macd"), errors="coerce").fillna(0) != 0).sum()) if "macd" in df.columns else -1,
            signal_nonzero=int((pd.to_numeric(df.get("signal"), errors="coerce").fillna(0) != 0).sum()) if "signal" in df.columns else -1,
            mtf_nonzero=int((pd.to_numeric(df.get("mtf"), errors="coerce").fillna(0) != 0).sum()) if "mtf" in df.columns else -1,
        )
        logger.warning(
            "[SUMMARY SEED MERGED GUARD] publish interval=%s history_rows=%s merged_push_rows=%s merged_ranking_rows=%s latest_dt=%s push_macd=%s push_signal=%s",
            interval,
            len(df),
            len(push_df),
            len(ranking_df),
            stats.get("latest_dt"),
            _nonzero_count(push_df, "macd"),
            _nonzero_count(push_df, "signal"),
        )
        return stats
    except Exception:
        logger.exception("[SUMMARY SEED MERGED GUARD] patched publish failed interval=%s -> fallback original", interval)
        if callable(_ORIGINAL_PUBLISH):
            return _ORIGINAL_PUBLISH(interval, df)
        return stats


def install() -> bool:
    global _PATCHED, _ORIGINAL_PUBLISH
    if _PATCHED:
        return True
    if not _env_bool("SUMMARY_SEED_RECENT_MERGED_GUARD_ENABLED", True):
        logger.warning("[SUMMARY SEED MERGED GUARD] disabled by env")
        return False
    try:
        import core.startup.summary_db_seed_restore_patch as seed_mod
        cur = getattr(seed_mod, "_publish_interval", None)
        if not callable(cur):
            logger.warning("[SUMMARY SEED MERGED GUARD] target _publish_interval not callable")
            return False
        if getattr(cur, "_summary_seed_recent_merged_guard", False):
            _PATCHED = True
            return True
        _ORIGINAL_PUBLISH = cur
        _patched_publish_interval._summary_seed_recent_merged_guard = True  # type: ignore[attr-defined]
        _patched_publish_interval._original = cur  # type: ignore[attr-defined]
        seed_mod._publish_interval = _patched_publish_interval
        _PATCHED = True
        logger.warning(
            "[SUMMARY SEED MERGED GUARD] installed max_age_min=%.1f",
            _env_float("SUMMARY_SEED_MERGED_MAX_AGE_MIN", 240.0),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY SEED MERGED GUARD] install failed")
        return False


__all__ = ["install"]
