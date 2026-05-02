# ============================================================
# File   : trading/ranking_summary/cache.py
# Version: Ver31_L23-RANKING-SUMMARY-CACHE-SEPARATED
# ------------------------------------------------------------
# 機能:
#   - ランキング由来サマリー専用キャッシュ管理
#   - interval別のDataFrame保存/取得
#   - interval別の最新datetime保存/取得
#   - global_data上で PUSH系キャッシュと完全分離
#
# 目的:
#   - ランキング由来サマリーとPUSH由来サマリーの混線防止
#   - ranking summary専用の取得口を提供する
#
# 主な関数:
#   - set_ranking_summary(interval, df)
#   - get_ranking_summary(interval)
#   - set_ranking_summary_latest_dt(interval, dt_value)
#   - get_ranking_summary_latest_dt(interval)
#   - clear_ranking_summary(interval=None)
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Union

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from global_state import global_data
except Exception:  # pragma: no cover
    global_data = None


IntervalLike = Union[int, str]


def _normalize_interval(interval: IntervalLike) -> str:
    """
    interval を '1min' / '3min' / '5min' のようなキーへ正規化する
    """
    try:
        s = str(interval).strip().lower().replace(" ", "")
        if s.endswith("min"):
            n = int(s[:-3])
        else:
            n = int(s)
        if n <= 0:
            n = 1
        return f"{n}min"
    except Exception:
        logger.exception("[RANKING CACHE] interval normalize failed interval=%r", interval)
        return "1min"


def _ensure_storage() -> None:
    """
    global_data 上に ランキング専用キャッシュ領域を確保する
    """
    global global_data

    if global_data is None:
        raise RuntimeError("global_data is not available")

    if not hasattr(global_data, "ranking_summary_cache"):
        global_data.ranking_summary_cache = {}

    if not hasattr(global_data, "ranking_summary_latest_dt"):
        global_data.ranking_summary_latest_dt = {}

    if not hasattr(global_data, "ranking_summary_meta"):
        global_data.ranking_summary_meta = {}


def set_ranking_summary(interval: IntervalLike, df: Optional[pd.DataFrame]) -> None:
    """
    ランキング由来サマリーDataFrameを interval別に保存する
    """
    try:
        _ensure_storage()
        key = _normalize_interval(interval)

        if isinstance(df, pd.DataFrame):
            out = df.copy()
        else:
            out = pd.DataFrame()

        global_data.ranking_summary_cache[key] = out

        rows = len(out) if isinstance(out, pd.DataFrame) else 0
        cols = list(out.columns)[:20] if isinstance(out, pd.DataFrame) else []
        logger.info(
            "[RANKING CACHE] set summary interval=%s rows=%s cols=%s",
            key,
            rows,
            cols,
        )

    except Exception:
        logger.exception("[RANKING CACHE] set_ranking_summary failed interval=%r", interval)


def get_ranking_summary(interval: IntervalLike) -> pd.DataFrame:
    """
    ランキング由来サマリーDataFrameを interval別に取得する
    """
    try:
        _ensure_storage()
        key = _normalize_interval(interval)

        df = global_data.ranking_summary_cache.get(key)
        if isinstance(df, pd.DataFrame):
            return df.copy()

        return pd.DataFrame()

    except Exception:
        logger.exception("[RANKING CACHE] get_ranking_summary failed interval=%r", interval)
        return pd.DataFrame()


def set_ranking_summary_latest_dt(interval: IntervalLike, dt_value) -> None:
    """
    ランキング由来サマリーの最新datetimeを interval別に保存する
    """
    try:
        _ensure_storage()
        key = _normalize_interval(interval)
        global_data.ranking_summary_latest_dt[key] = dt_value
        logger.info("[RANKING CACHE] set latest_dt interval=%s value=%r", key, dt_value)
    except Exception:
        logger.exception(
            "[RANKING CACHE] set_ranking_summary_latest_dt failed interval=%r",
            interval,
        )


def get_ranking_summary_latest_dt(interval: IntervalLike):
    """
    ランキング由来サマリーの最新datetimeを interval別に取得する
    """
    try:
        _ensure_storage()
        key = _normalize_interval(interval)
        return global_data.ranking_summary_latest_dt.get(key)
    except Exception:
        logger.exception(
            "[RANKING CACHE] get_ranking_summary_latest_dt failed interval=%r",
            interval,
        )
        return None


def set_ranking_summary_meta(interval: IntervalLike, meta: dict) -> None:
    """
    ランキング由来サマリーの補助メタ情報を保存する
    """
    try:
        _ensure_storage()
        key = _normalize_interval(interval)
        global_data.ranking_summary_meta[key] = dict(meta or {})
        logger.info("[RANKING CACHE] set meta interval=%s keys=%s", key, list((meta or {}).keys()))
    except Exception:
        logger.exception("[RANKING CACHE] set_ranking_summary_meta failed interval=%r", interval)


def get_ranking_summary_meta(interval: IntervalLike) -> dict:
    """
    ランキング由来サマリーの補助メタ情報を取得する
    """
    try:
        _ensure_storage()
        key = _normalize_interval(interval)
        value = global_data.ranking_summary_meta.get(key)
        return dict(value or {})
    except Exception:
        logger.exception("[RANKING CACHE] get_ranking_summary_meta failed interval=%r", interval)
        return {}


def clear_ranking_summary(interval: Optional[IntervalLike] = None) -> None:
    """
    ランキング由来サマリーキャッシュを削除する
    interval=None の場合は全削除
    """
    try:
        _ensure_storage()

        if interval is None:
            global_data.ranking_summary_cache = {}
            global_data.ranking_summary_latest_dt = {}
            global_data.ranking_summary_meta = {}
            logger.info("[RANKING CACHE] cleared all")
            return

        key = _normalize_interval(interval)
        global_data.ranking_summary_cache.pop(key, None)
        global_data.ranking_summary_latest_dt.pop(key, None)
        global_data.ranking_summary_meta.pop(key, None)
        logger.info("[RANKING CACHE] cleared interval=%s", key)

    except Exception:
        logger.exception("[RANKING CACHE] clear_ranking_summary failed interval=%r", interval)