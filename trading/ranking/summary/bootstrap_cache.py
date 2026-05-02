# ============================================================
# File   : trading/ranking/summary/bootstrap_cache.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-CACHE
# ------------------------------------------------------------
# 【概要】
#   ranking summary を global_data に反映
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def get_global_data() -> Any:
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


def set_global_cache(interval: int, df: pd.DataFrame) -> None:
    gd = get_global_data()

    if gd is None:
        logger.warning(
            "[RANKING SUMMARY BOOTSTRAP CACHE] global_data not found interval=%s rows=%d",
            interval,
            len(df) if isinstance(df, pd.DataFrame) else 0,
        )
        return

    keys = [
        f"ranking_summary_{interval}min",
        f"ranking_summary_{interval}",
        f"ranking_summary_df_{interval}",
        f"ranking_merged_summary_{interval}min",
    ]

    for key in keys:
        try:
            setattr(gd, key, df.copy())
        except Exception:
            pass

    setter_candidates = [
        "set_ranking_summary",
        "set_ranking_summary_df",
        "set_ranking_merged_summary",
        "set_summary",
        "set_merged_summary",
    ]

    for name in setter_candidates:
        try:
            fn = getattr(gd, name, None)
            if not callable(fn):
                continue

            try:
                fn(interval, df.copy())
                logger.info(
                    "[RANKING SUMMARY BOOTSTRAP CACHE] global_data.%s interval=%s rows=%d",
                    name,
                    interval,
                    len(df),
                )
                return
            except TypeError:
                try:
                    fn(f"{interval}min", df.copy())
                    logger.info(
                        "[RANKING SUMMARY BOOTSTRAP CACHE] global_data.%s tf=%smin rows=%d",
                        name,
                        interval,
                        len(df),
                    )
                    return
                except Exception:
                    pass
        except Exception:
            pass

    logger.info(
        "[RANKING SUMMARY BOOTSTRAP CACHE] assigned by attributes interval=%s rows=%d",
        interval,
        len(df),
    )


__all__ = [
    "get_global_data",
    "set_global_cache",
]