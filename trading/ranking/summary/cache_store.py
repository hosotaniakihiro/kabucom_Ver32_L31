# ============================================================
# File   : trading/ranking/summary/cache_store.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-CACHE-STORE
# ------------------------------------------------------------
# ranking summary cache / latest cache / global_data slot 管理
# ranking_summary_engine.py から安全に切り出すためのモジュール
# ============================================================

from __future__ import annotations

import logging
from typing import Dict

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# global_data 互換解決
# ============================================================

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()


# ============================================================
# local cache fallback
# global_data が揮発しても履歴維持
# ============================================================

_LOCAL_RANKING_SUMMARY_CACHE: dict[int, pd.DataFrame] = {
    1: pd.DataFrame(),
    3: pd.DataFrame(),
    5: pd.DataFrame(),
}

_LOCAL_LATEST_RANKING_SUMMARY_CACHE: dict[int, pd.DataFrame] = {
    1: pd.DataFrame(),
    3: pd.DataFrame(),
    5: pd.DataFrame(),
}


# ============================================================
# global slots
# ============================================================

def _ensure_global_slots() -> None:
    if not hasattr(global_data, "ranking_summary_by_interval"):
        global_data.ranking_summary_by_interval = {}

    if not hasattr(global_data, "latest_ranking_summary_by_interval"):
        global_data.latest_ranking_summary_by_interval = {}

    if not hasattr(global_data, "ranking_summary_last_announced_dt"):
        global_data.ranking_summary_last_announced_dt = {}

    if not hasattr(global_data, "ranking_summary_universe"):
        global_data.ranking_summary_universe = []

    if not hasattr(global_data, "ranking_summary_initialized"):
        global_data.ranking_summary_initialized = False

    if not hasattr(global_data, "ranking_summary_runtime_filter_enabled"):
        global_data.ranking_summary_runtime_filter_enabled = False

    if not hasattr(global_data, "ranking_summary_last_runtime_symbols"):
        global_data.ranking_summary_last_runtime_symbols = []

    if not hasattr(global_data, "ranking_summary_status_meta"):
        global_data.ranking_summary_status_meta = {}

    if not hasattr(global_data, "ranking_summary_use_universe_filter"):
        global_data.ranking_summary_use_universe_filter = False


# ============================================================
# basic helpers
# ============================================================

def _safe_df(df) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        try:
            return df.copy()
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _safe_interval(interval: int) -> int:
    try:
        iv = int(interval)
    except Exception:
        iv = 1

    if iv not in (1, 3, 5):
        logger.warning("[RANKING SUMMARY CACHE] unsupported interval=%s -> use raw key", iv)

    return iv


# ============================================================
# ranking summary cache
# ============================================================

def get_ranking_summary(interval: int) -> pd.DataFrame:
    _ensure_global_slots()
    interval = _safe_interval(interval)

    try:
        data = getattr(global_data, "ranking_summary_by_interval", {}) or {}
        df = data.get(interval)

        if isinstance(df, pd.DataFrame) and not df.empty:
            try:
                _LOCAL_RANKING_SUMMARY_CACHE[int(interval)] = df.copy()
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY CACHE] local cache sync failed interval=%s",
                    interval,
                )
            return df.copy()

        local_df = _LOCAL_RANKING_SUMMARY_CACHE.get(int(interval))
        if isinstance(local_df, pd.DataFrame) and not local_df.empty:
            logger.warning(
                "[RANKING SUMMARY CACHE] global cache empty -> local cache fallback interval=%s rows=%d",
                interval,
                len(local_df),
            )
            return local_df.copy()

        return pd.DataFrame()

    except Exception:
        logger.exception(
            "[RANKING SUMMARY CACHE] get cache failed interval=%s",
            interval,
        )
        local_df = _LOCAL_RANKING_SUMMARY_CACHE.get(int(interval))
        return local_df.copy() if isinstance(local_df, pd.DataFrame) else pd.DataFrame()


def set_ranking_summary(interval: int, df: pd.DataFrame) -> None:
    _ensure_global_slots()
    interval = _safe_interval(interval)

    try:
        safe_df = _safe_df(df)

        _LOCAL_RANKING_SUMMARY_CACHE[int(interval)] = safe_df.copy()

        data = dict(getattr(global_data, "ranking_summary_by_interval", {}) or {})
        data[int(interval)] = safe_df.copy()
        global_data.ranking_summary_by_interval = data

        logger.info(
            "[RANKING SUMMARY CACHE] cache set interval=%s rows=%d",
            interval,
            len(safe_df),
        )

    except Exception:
        logger.exception(
            "[RANKING SUMMARY CACHE] set cache failed interval=%s",
            interval,
        )
        try:
            _LOCAL_RANKING_SUMMARY_CACHE[int(interval)] = _safe_df(df)
        except Exception:
            logger.exception(
                "[RANKING SUMMARY CACHE] local cache fallback set failed interval=%s",
                interval,
            )


# ============================================================
# latest ranking summary cache
# ============================================================

def get_latest_ranking_summary(interval: int) -> pd.DataFrame:
    _ensure_global_slots()
    interval = _safe_interval(interval)

    try:
        data = getattr(global_data, "latest_ranking_summary_by_interval", {}) or {}
        df = data.get(interval)

        if isinstance(df, pd.DataFrame) and not df.empty:
            try:
                _LOCAL_LATEST_RANKING_SUMMARY_CACHE[int(interval)] = df.copy()
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY CACHE] local latest cache sync failed interval=%s",
                    interval,
                )
            return df.copy()

        local_df = _LOCAL_LATEST_RANKING_SUMMARY_CACHE.get(int(interval))
        if isinstance(local_df, pd.DataFrame) and not local_df.empty:
            logger.warning(
                "[RANKING SUMMARY CACHE] global latest cache empty -> local latest fallback interval=%s rows=%d",
                interval,
                len(local_df),
            )
            return local_df.copy()

        return pd.DataFrame()

    except Exception:
        logger.exception(
            "[RANKING SUMMARY CACHE] get latest cache failed interval=%s",
            interval,
        )
        local_df = _LOCAL_LATEST_RANKING_SUMMARY_CACHE.get(int(interval))
        return local_df.copy() if isinstance(local_df, pd.DataFrame) else pd.DataFrame()


def set_latest_ranking_summary(interval: int, df: pd.DataFrame) -> None:
    _ensure_global_slots()
    interval = _safe_interval(interval)

    try:
        safe_df = _safe_df(df)

        _LOCAL_LATEST_RANKING_SUMMARY_CACHE[int(interval)] = safe_df.copy()

        data = dict(getattr(global_data, "latest_ranking_summary_by_interval", {}) or {})
        data[int(interval)] = safe_df.copy()
        global_data.latest_ranking_summary_by_interval = data

        logger.info(
            "[RANKING SUMMARY CACHE] latest cache set interval=%s rows=%d",
            interval,
            len(safe_df),
        )

    except Exception:
        logger.exception(
            "[RANKING SUMMARY CACHE] set latest cache failed interval=%s",
            interval,
        )
        try:
            _LOCAL_LATEST_RANKING_SUMMARY_CACHE[int(interval)] = _safe_df(df)
        except Exception:
            logger.exception(
                "[RANKING SUMMARY CACHE] local latest cache fallback set failed interval=%s",
                interval,
            )


# ============================================================
# status helpers
# ============================================================

def get_ranking_summary_initialized() -> bool:
    _ensure_global_slots()
    try:
        return bool(getattr(global_data, "ranking_summary_initialized", False))
    except Exception:
        return False


def set_ranking_summary_initialized(value: bool) -> None:
    _ensure_global_slots()
    try:
        global_data.ranking_summary_initialized = bool(value)
    except Exception:
        logger.exception("[RANKING SUMMARY CACHE] set initialized failed")


def get_ranking_summary_status_meta() -> Dict:
    _ensure_global_slots()
    try:
        meta = getattr(global_data, "ranking_summary_status_meta", {}) or {}
        return dict(meta)
    except Exception:
        logger.exception("[RANKING SUMMARY CACHE] get status meta failed")
        return {}


def set_ranking_summary_status_meta(meta: Dict) -> None:
    _ensure_global_slots()
    try:
        global_data.ranking_summary_status_meta = dict(meta or {})
    except Exception:
        logger.exception("[RANKING SUMMARY CACHE] set status meta failed")


__all__ = [
    "_ensure_global_slots",
    "get_ranking_summary",
    "set_ranking_summary",
    "get_latest_ranking_summary",
    "set_latest_ranking_summary",
    "get_ranking_summary_initialized",
    "set_ranking_summary_initialized",
    "get_ranking_summary_status_meta",
    "set_ranking_summary_status_meta",
]