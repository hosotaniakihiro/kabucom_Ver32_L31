# ============================================================
# File   : core/startup/summary_mtf_diff_from_1m_patch.py
# Version: V1-INSTALL-MTF-DIFF-FROM-1M-DB
# ------------------------------------------------------------
# 目的:
#   3分足/5分足のサマリー更新時に、既存3m/5m最新時刻以降の1分足をDBから読み、
#   MA75計算用の直前履歴込みで差分3m/5mサマリーを作成・保存する。
#
# ポイント:
#   - 3m/5mの最新保存済みdatetimeを確認
#   - その直前74本 + それ以降の1mから作った差分バーを連結
#   - indicator/scoringを再計算
#   - DB保存は差分バーのみ
#   - original diff_update はその後も実行し、表示/entryの既存流れは維持
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_DIFF_UPDATE = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_len(x: Any) -> int:
    try:
        return 0 if x is None else len(x)
    except Exception:
        return 0


def _normalize_summary_df(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    try:
        from trading.summary.controller_utils import normalize_summary_df
        out = normalize_summary_df(df)
    except Exception:
        out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out is None or out.empty:
        return pd.DataFrame()
    try:
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.dropna(subset=["datetime"])
        out["interval"] = int(interval)
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] normalize failed interval=%s", interval)
        return pd.DataFrame()


def _publish_to_global(interval: int, df_hist: pd.DataFrame, df_latest: pd.DataFrame) -> None:
    try:
        from global_state import global_data
        try:
            global_data.set_push_merged_summary(interval, df_hist.copy())
        except Exception:
            logger.debug("[SUMMARY MTF DIFF 1M PATCH] set_push_merged_summary unavailable interval=%s", interval, exc_info=True)
        try:
            setattr(global_data, f"summary_{interval}m_df", df_hist.copy())
        except Exception:
            pass
        try:
            setter = getattr(global_data, "set_summary_history", None)
            if callable(setter):
                setter(interval, df_hist.copy())
        except Exception:
            pass
        try:
            setter = getattr(global_data, "set_latest_summary", None)
            if callable(setter):
                setter(interval, df_latest.copy())
        except Exception:
            pass
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] global publish failed interval=%s", interval)


def _run_diff_from_1m(interval: int) -> pd.DataFrame:
    if int(interval) not in (3, 5):
        return pd.DataFrame()
    if not _env_bool("SUMMARY_MTF_DIFF_FROM_1M_ENABLED", True):
        return pd.DataFrame()

    try:
        from trading.summary.pipeline.incremental_mtf_from_1min import (
            build_incremental_mtf_from_1m,
            extract_diff_rows,
        )
        from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
        from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
        from trading.summary.persistence.summary_persistence import save_summary

        built = build_incremental_mtf_from_1m(interval)
        if not isinstance(built, dict) or not built.get("ok"):
            logger.info(
                "[SUMMARY MTF DIFF 1M PATCH] no diff interval=%s reason=%s one_raw_rows=%s latest_dt=%s",
                interval,
                built.get("reason") if isinstance(built, dict) else None,
                built.get("one_raw_rows") if isinstance(built, dict) else None,
                built.get("latest_dt") if isinstance(built, dict) else None,
            )
            return pd.DataFrame()

        hist_df = _normalize_summary_df(built.get("history_df"), interval=interval)
        diff_seed = _normalize_summary_df(built.get("diff_df"), interval=interval)
        if hist_df.empty or diff_seed.empty:
            return pd.DataFrame()

        df_hist = run_indicator_pipeline(hist_df.copy(), interval)
        df_hist = _normalize_summary_df(df_hist, interval=interval)
        df_hist = run_scoring_pipeline(df_hist, interval=f"{int(interval)}min")
        df_hist = _normalize_summary_df(df_hist, interval=interval)

        diff_rows = extract_diff_rows(df_hist, diff_seed, interval=interval)
        diff_rows = _normalize_summary_df(diff_rows, interval=interval)
        if diff_rows.empty:
            logger.info("[SUMMARY MTF DIFF 1M PATCH] diff rows empty after indicator/scoring interval=%s", interval)
            return pd.DataFrame()

        save_summary(
            diff_rows,
            int(interval),
            lock_timeout_sec=3.0,
            skip_if_busy=True,
            caller="summary_mtf_diff_from_1m_patch",
        )

        try:
            latest = diff_rows.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
        except Exception:
            latest = diff_rows.copy()
        _publish_to_global(int(interval), df_hist, latest)

        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] saved interval=%s diff_rows=%s diff_symbols=%s hist_rows=%s latest_dt=%s path=%s",
            interval,
            _safe_len(diff_rows),
            int(diff_rows["symbol"].nunique()) if "symbol" in diff_rows.columns else 0,
            _safe_len(df_hist),
            built.get("latest_dt"),
            built.get("path"),
        )
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] failed interval=%s", interval)
        return pd.DataFrame()


def _patched_diff_update(self, interval: int, *args, **kwargs):
    interval_i = int(interval)
    precomputed_latest = pd.DataFrame()
    if interval_i in (3, 5):
        precomputed_latest = _run_diff_from_1m(interval_i)

    try:
        return _ORIG_DIFF_UPDATE(self, interval_i, *args, **kwargs)  # type: ignore[misc]
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] original diff_update failed interval=%s", interval_i)
        if isinstance(precomputed_latest, pd.DataFrame) and not precomputed_latest.empty:
            return precomputed_latest
        return pd.DataFrame()


def install() -> bool:
    global _INSTALLED, _ORIG_DIFF_UPDATE
    if _INSTALLED:
        return True
    try:
        if not _env_bool("SUMMARY_MTF_DIFF_FROM_1M_ENABLED", True):
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] disabled by env")
            return False

        import trading.summary.summary_controller as sc
        cls = getattr(sc, "SummaryController", None)
        if cls is None:
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] SummaryController unavailable")
            return False
        cur = getattr(cls, "diff_update", None)
        if not callable(cur):
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] diff_update unavailable")
            return False
        if getattr(cur, "_summary_mtf_diff_from_1m_v1", False):
            _INSTALLED = True
            return True

        _ORIG_DIFF_UPDATE = cur
        _patched_diff_update._summary_mtf_diff_from_1m_v1 = True  # type: ignore[attr-defined]
        _patched_diff_update._original = cur  # type: ignore[attr-defined]
        cls.diff_update = _patched_diff_update
        _INSTALLED = True
        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] installed enabled=True history_rows=%s allow_partial=%s",
            os.getenv("SUMMARY_MTF_DIFF_HISTORY_ROWS", "74"),
            os.getenv("SUMMARY_MTF_DIFF_ALLOW_PARTIAL_BAR", "0"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MTF DIFF 1M PATCH] auto install failed")

__all__ = ["install"]
