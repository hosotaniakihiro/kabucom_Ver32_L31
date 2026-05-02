# ============================================================
# File   : trading/summary/top_candidates_pkg/resolvers.py
# Version: Ver2.2-PRODUCTION-SUMMARY-TOP-CANDIDATES-RESOLVERS
# ------------------------------------------------------------
# Function:
#   - global_data から PUSH由来サマリーを取得
#   - global_data からランキング由来サマリーを取得
#   - core.global_context.context.GlobalContext Rev10.1 互換
# ------------------------------------------------------------
# PUSH 取得優先:
#   1. get_merged_summary(tf, source="push")
#   2. get_push_merged_summary(tf)
#   3. get_push_summary(tf)
#   4. push_summary_cache[tf]
#   5. merged_summary_{tf}
#   6. push_summary_{tf}
#   7. fallback get_merged_summary(tf)
# ------------------------------------------------------------
# Ranking 取得優先:
#   1. get_merged_summary(tf, source="ranking")
#   2. get_ranking_merged_summary(tf)
#   3. get_ranking_summary(tf)
#   4. ranking_summary_cache[tf]
#   5. ranking_summary_{tf}
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .utils import (
    ensure_dataframe,
    is_completed_summary_like,
    tf_candidates,
)

logger = logging.getLogger(__name__)


def get_global_data() -> Any:
    """
    global_data を安全に解決する。

    対応:
      - global_state.global_data
      - core.global_context.context.global_data
    """

    try:
        from global_state import global_data  # type: ignore
        return global_data
    except Exception:
        pass

    try:
        from core.global_context.context import global_data  # type: ignore
        return global_data
    except Exception:
        return None


def return_if_valid_df(
    df: Any,
    *,
    label: str,
    interval: int,
    source_kind: str,
) -> pd.DataFrame:
    """
    取得した df が候補抽出に使えるなら返す。
    """

    out = ensure_dataframe(df)

    if out.empty:
        logger.debug(
            "[TOP RESOLVER] %s empty source=%s interval=%s",
            source_kind,
            label,
            interval,
        )
        return pd.DataFrame()

    if not is_completed_summary_like(out):
        logger.warning(
            "[TOP RESOLVER] %s rejected incomplete source=%s interval=%s rows=%d cols=%s",
            source_kind,
            label,
            interval,
            len(out),
            list(out.columns),
        )
        return pd.DataFrame()

    logger.info(
        "[TOP RESOLVER] %s resolved source=%s interval=%s rows=%d cols=%s",
        source_kind,
        label,
        interval,
        len(out),
        list(out.columns[:20]),
    )

    return out


def call_get_merged_summary_with_source(
    gd: Any,
    *,
    tf: Any,
    source: str,
) -> pd.DataFrame:
    """
    GlobalContext.get_merged_summary(tf, source="push/ranking") 対応。

    古い実装で source キーワード未対応の可能性もあるため、
    TypeError 時は空で返す。
    """

    try:
        fn = getattr(gd, "get_merged_summary", None)

        if not callable(fn):
            return pd.DataFrame()

        return ensure_dataframe(fn(tf, source=source))

    except TypeError:
        logger.debug(
            "[TOP RESOLVER] get_merged_summary source kw unsupported tf=%s source=%s",
            tf,
            source,
            exc_info=True,
        )
        return pd.DataFrame()

    except Exception:
        logger.debug(
            "[TOP RESOLVER] get_merged_summary failed tf=%s source=%s",
            tf,
            source,
            exc_info=True,
        )
        return pd.DataFrame()


def get_cache_df(
    gd: Any,
    *,
    cache_name: str,
    interval: int,
) -> pd.DataFrame:
    """
    global_data.push_summary_cache / ranking_summary_cache から取得。
    """

    try:
        cache = getattr(gd, cache_name, None)

        if not isinstance(cache, dict):
            return pd.DataFrame()

        candidates = [
            interval,
            str(interval),
            f"{interval}m",
            f"{interval}min",
        ]

        for key in candidates:
            df = ensure_dataframe(cache.get(key))
            if not df.empty:
                return df

        return pd.DataFrame()

    except Exception:
        logger.debug(
            "[TOP RESOLVER] cache get failed cache=%s interval=%s",
            cache_name,
            interval,
            exc_info=True,
        )
        return pd.DataFrame()


def get_push_summary_df(interval: int) -> pd.DataFrame:
    """
    PUSH由来サマリーを global_data から取得。
    """

    gd = get_global_data()

    if gd is None:
        logger.warning("[TOP RESOLVER] global_data not available for push summary")
        return pd.DataFrame()

    interval = int(interval)
    tf_list = tf_candidates(interval)

    # 1. get_merged_summary(tf, source="push")
    for tf in tf_list:
        df = call_get_merged_summary_with_source(gd, tf=tf, source="push")
        out = return_if_valid_df(
            df,
            label=f"get_merged_summary({tf}, source='push')",
            interval=interval,
            source_kind="push",
        )
        if not out.empty:
            return out

    # 2. get_push_merged_summary(tf)
    fn = getattr(gd, "get_push_merged_summary", None)
    if callable(fn):
        for tf in tf_list:
            try:
                df = fn(tf)
                out = return_if_valid_df(
                    df,
                    label=f"get_push_merged_summary({tf})",
                    interval=interval,
                    source_kind="push",
                )
                if not out.empty:
                    return out
            except Exception:
                logger.debug(
                    "[TOP RESOLVER] get_push_merged_summary failed tf=%s",
                    tf,
                    exc_info=True,
                )

    # 3. get_push_summary(tf)
    fn = getattr(gd, "get_push_summary", None)
    if callable(fn):
        for tf in tf_list:
            try:
                df = fn(tf)
                out = return_if_valid_df(
                    df,
                    label=f"get_push_summary({tf})",
                    interval=interval,
                    source_kind="push",
                )
                if not out.empty:
                    return out
            except Exception:
                logger.debug(
                    "[TOP RESOLVER] get_push_summary failed tf=%s",
                    tf,
                    exc_info=True,
                )

    # 4. push_summary_cache[tf]
    df = get_cache_df(
        gd,
        cache_name="push_summary_cache",
        interval=interval,
    )
    out = return_if_valid_df(
        df,
        label=f"push_summary_cache[{interval}]",
        interval=interval,
        source_kind="push",
    )
    if not out.empty:
        return out

    # 5. legacy attrs
    attr_names = [
        f"merged_summary_{interval}",
        f"merged_summary_{interval}min",
        f"push_summary_{interval}",
        f"push_summary_{interval}min",
        f"summary_{interval}",
        f"summary_{interval}min",
    ]

    for attr in attr_names:
        try:
            df = getattr(gd, attr, None)
            out = return_if_valid_df(
                df,
                label=attr,
                interval=interval,
                source_kind="push",
            )
            if not out.empty:
                return out
        except Exception:
            logger.debug(
                "[TOP RESOLVER] push summary attr failed attr=%s",
                attr,
                exc_info=True,
            )

    # 6. 最後だけ source 未指定 fallback
    fn = getattr(gd, "get_merged_summary", None)
    if callable(fn):
        for tf in tf_list:
            try:
                df = fn(tf)
                out = return_if_valid_df(
                    df,
                    label=f"get_merged_summary({tf}) fallback",
                    interval=interval,
                    source_kind="push",
                )
                if not out.empty:
                    logger.warning(
                        "[TOP RESOLVER] push used source-unspecified fallback interval=%s tf=%s",
                        interval,
                        tf,
                    )
                    return out
            except Exception:
                logger.debug(
                    "[TOP RESOLVER] get_merged_summary fallback failed tf=%s",
                    tf,
                    exc_info=True,
                )

    logger.warning("[TOP RESOLVER] push summary unresolved interval=%s", interval)
    return pd.DataFrame()


def get_ranking_summary_df(interval: int) -> pd.DataFrame:
    """
    ランキング由来サマリーを global_data から取得。

    注意:
      - get_merged_ranking_summary は context.py には存在しないため使わない。
      - source 未指定 get_merged_summary(tf) は push を拾う可能性があるため使わない。
    """

    gd = get_global_data()

    if gd is None:
        logger.warning("[TOP RESOLVER] global_data not available for ranking summary")
        return pd.DataFrame()

    interval = int(interval)
    tf_list = tf_candidates(interval)

    # 1. get_merged_summary(tf, source="ranking")
    for tf in tf_list:
        df = call_get_merged_summary_with_source(gd, tf=tf, source="ranking")
        out = return_if_valid_df(
            df,
            label=f"get_merged_summary({tf}, source='ranking')",
            interval=interval,
            source_kind="ranking",
        )
        if not out.empty:
            return out

    # 2. get_ranking_merged_summary(tf)
    fn = getattr(gd, "get_ranking_merged_summary", None)
    if callable(fn):
        for tf in tf_list:
            try:
                df = fn(tf)
                out = return_if_valid_df(
                    df,
                    label=f"get_ranking_merged_summary({tf})",
                    interval=interval,
                    source_kind="ranking",
                )
                if not out.empty:
                    return out
            except Exception:
                logger.debug(
                    "[TOP RESOLVER] get_ranking_merged_summary failed tf=%s",
                    tf,
                    exc_info=True,
                )

    # 3. get_ranking_summary(tf)
    fn = getattr(gd, "get_ranking_summary", None)
    if callable(fn):
        for tf in tf_list:
            try:
                df = fn(tf)
                out = return_if_valid_df(
                    df,
                    label=f"get_ranking_summary({tf})",
                    interval=interval,
                    source_kind="ranking",
                )
                if not out.empty:
                    return out
            except Exception:
                logger.debug(
                    "[TOP RESOLVER] get_ranking_summary failed tf=%s",
                    tf,
                    exc_info=True,
                )

    # 4. ranking_summary_cache[tf]
    df = get_cache_df(
        gd,
        cache_name="ranking_summary_cache",
        interval=interval,
    )
    out = return_if_valid_df(
        df,
        label=f"ranking_summary_cache[{interval}]",
        interval=interval,
        source_kind="ranking",
    )
    if not out.empty:
        return out

    # 5. legacy attrs
    attr_names = [
        f"ranking_summary_{interval}",
        f"ranking_summary_{interval}min",
        f"ranking_merged_summary_{interval}",
        f"ranking_merged_summary_{interval}min",
        f"merged_ranking_summary_{interval}",
        f"merged_ranking_summary_{interval}min",
    ]

    for attr in attr_names:
        try:
            df = getattr(gd, attr, None)
            out = return_if_valid_df(
                df,
                label=attr,
                interval=interval,
                source_kind="ranking",
            )
            if not out.empty:
                return out
        except Exception:
            logger.debug(
                "[TOP RESOLVER] ranking summary attr failed attr=%s",
                attr,
                exc_info=True,
            )

    logger.warning("[TOP RESOLVER] ranking summary unresolved interval=%s", interval)
    return pd.DataFrame()