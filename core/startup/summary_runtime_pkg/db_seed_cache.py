# ============================================================
# File   : core/startup/summary_runtime_pkg/db_seed_cache.py
# Version: REV1.0-SUMMARY-RUNTIME-DB-SEED-CACHE-COMPAT
# ------------------------------------------------------------
# 【概要】
#   起動時 summary DB seed 用 cache accessor
#
# 【主な機能】
#   ✔ merged_summary_access.py 互換
#   ✔ get_summary_history_safe / set_summary_history_safe が無い環境に対応
#   ✔ global_data 直接 fallback
#   ✔ summary_history_cache dict fallback
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


from core.startup.merged_summary_access import (
    get_push_merged_summary_safe,
    set_push_merged_summary_safe,
)

try:
    from core.startup.merged_summary_access import (
        get_summary_history_safe as _external_get_summary_history_safe,
        set_summary_history_safe as _external_set_summary_history_safe,
    )
except Exception:
    _external_get_summary_history_safe = None
    _external_set_summary_history_safe = None


def get_summary_history_safe(tf: int) -> pd.DataFrame:
    """
    summary history cache 互換 getter。

    merged_summary_access.py に get_summary_history_safe がある場合はそれを使う。
    無い場合は global_data から直接読む。
    """
    try:
        if callable(_external_get_summary_history_safe):
            df = _external_get_summary_history_safe(tf)
            if isinstance(df, pd.DataFrame):
                return df
    except Exception:
        logger.debug(
            "[summary_runtime] external get_summary_history_safe failed tf=%s",
            tf,
            exc_info=True,
        )

    try:
        from core.global_context.context import global_data

        candidates = [
            "get_summary_history",
            "get_summary_history_df",
            "get_history_summary",
        ]

        for name in candidates:
            fn = getattr(global_data, name, None)
            if callable(fn):
                try:
                    df = fn(tf)
                except TypeError:
                    try:
                        df = fn(int(tf), source="push")
                    except TypeError:
                        df = fn(tf, "push")

                if isinstance(df, pd.DataFrame):
                    return df

        for attr in [
            "summary_history",
            "summary_history_cache",
            "push_summary_history",
            "_summary_history",
            "_summary_history_cache",
        ]:
            obj = getattr(global_data, attr, None)
            if isinstance(obj, dict):
                for key in [
                    tf,
                    int(tf),
                    str(tf),
                    f"{int(tf)}min",
                    ("push", int(tf)),
                    (int(tf), "push"),
                ]:
                    df = obj.get(key)
                    if isinstance(df, pd.DataFrame):
                        return df

    except Exception:
        logger.debug(
            "[summary_runtime] fallback get_summary_history_safe failed tf=%s",
            tf,
            exc_info=True,
        )

    return pd.DataFrame()


def set_summary_history_safe(tf: int, df: pd.DataFrame) -> None:
    """
    summary history cache 互換 setter。

    merged_summary_access.py に set_summary_history_safe がある場合はそれを使う。
    無い場合は global_data へ直接保存する。
    """
    if not isinstance(df, pd.DataFrame):
        return

    try:
        if callable(_external_set_summary_history_safe):
            _external_set_summary_history_safe(tf, df)
            return
    except Exception:
        logger.debug(
            "[summary_runtime] external set_summary_history_safe failed tf=%s",
            tf,
            exc_info=True,
        )

    try:
        from core.global_context.context import global_data

        candidates = [
            "set_summary_history",
            "set_summary_history_df",
            "set_history_summary",
        ]

        for name in candidates:
            fn = getattr(global_data, name, None)
            if callable(fn):
                try:
                    fn(tf, df)
                except TypeError:
                    try:
                        fn(int(tf), df, source="push")
                    except TypeError:
                        fn(tf, "push", df)
                return

        obj = getattr(global_data, "summary_history_cache", None)
        if not isinstance(obj, dict):
            obj = {}
            setattr(global_data, "summary_history_cache", obj)

        obj[int(tf)] = df.copy()
        obj[str(tf)] = df.copy()
        obj[f"{int(tf)}min"] = df.copy()
        obj[("push", int(tf))] = df.copy()
        obj[(int(tf), "push")] = df.copy()

        logger.info(
            "[summary_runtime] fallback summary history stored tf=%s rows=%d symbols=%d",
            tf,
            len(df),
            int(df["symbol"].nunique()) if "symbol" in df.columns and not df.empty else 0,
        )

    except Exception:
        logger.debug(
            "[summary_runtime] fallback set_summary_history_safe failed tf=%s",
            tf,
            exc_info=True,
        )


__all__ = [
    "get_push_merged_summary_safe",
    "set_push_merged_summary_safe",
    "get_summary_history_safe",
    "set_summary_history_safe",
]