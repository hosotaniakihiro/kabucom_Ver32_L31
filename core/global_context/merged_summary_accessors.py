# ============================================================
# File   : core/startup/merged_summary_access.py
# Version: REV1.2-STARTUP-MERGED-SUMMARY-ACCESS
#          -SUMMARY-HISTORY-CACHE-CLEANED
# ------------------------------------------------------------
# 【概要】
#   startup から merged summary / summary history access を分離
#
# 【主な機能】
#   - global_data の source="push" 互換アクセス
#   - 旧 global_state / 旧 setter/getter にも後方互換
#   - 表示用 merged summary getter/setter
#   - 計算用 summary history getter/setter
#   - 汎用 merged summary getter/setter
#
# 【REV1.2 修正】
#   - get_summary_history_safe / set_summary_history_safe の
#     重複定義を削除
#   - __all__ の重複を削除
#   - tf 正規化を全 getter/setter で統一
#
# 【重要】
#   - set_push_merged_summary_safe():
#       表示用。GlobalContext.set_merged_summary() 側で
#       最新1行/銘柄へ圧縮される。
#
#   - set_summary_history_safe():
#       計算用。DB seed で読み込んだ履歴DFを保持する。
#       indicator / ranking / scoring が履歴本数を必要とする場合はこちらを見る。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# small helpers
# ============================================================

def _normalize_tf(tf: Any) -> int | str:
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


def _safe_copy_df(df):
    try:
        if isinstance(df, pd.DataFrame):
            return df.copy()
        return df
    except Exception:
        return df


def _is_nonempty_df(df) -> bool:
    try:
        return isinstance(df, pd.DataFrame) and not df.empty
    except Exception:
        return False


def _legacy_attr_name(tf: Any) -> str | None:
    tf = _normalize_tf(tf)

    mapping = {
        1: "merged_summary_1",
        3: "merged_summary_3",
        5: "merged_summary_5",
        10: "merged_summary_10",
        15: "merged_summary_15",
        30: "merged_summary_30",
        60: "merged_summary_60",
        "daily": "merged_summary_daily",
    }

    return mapping.get(tf)


# ============================================================
# display/latest merged summary access
# ============================================================

def get_push_merged_summary_safe(tf: int):
    """
    表示用 push merged summary を取得する。

    優先順位:
      1. global_data.get_merged_summary(tf, source="push")
      2. global_data.get_push_merged_summary(tf)
      3. global_data.merged_summary_{tf}
    """
    tf_norm = _normalize_tf(tf)

    try:
        getter = getattr(global_data, "get_merged_summary", None)
        if callable(getter):
            try:
                return getter(tf_norm, source="push")
            except TypeError:
                return getter(tf_norm)
    except Exception:
        logger.exception("[STARTUP] get_merged_summary failed tf=%s", tf_norm)

    try:
        getter = getattr(global_data, "get_push_merged_summary", None)
        if callable(getter):
            return getter(tf_norm)
    except Exception:
        logger.exception("[STARTUP] get_push_merged_summary failed tf=%s", tf_norm)

    try:
        attr = _legacy_attr_name(tf_norm)
        if attr:
            return getattr(global_data, attr, None)

        return getattr(global_data, f"merged_summary_{int(tf_norm)}", None)
    except Exception:
        logger.exception("[STARTUP] merged_summary attr fallback failed tf=%s", tf_norm)
        return None


def set_push_merged_summary_safe(tf: int, df) -> None:
    """
    表示用 push merged summary に保存する。

    注意:
      GlobalContext.set_merged_summary() 側では、
      表示用として最新1行/銘柄へ圧縮される。
    """
    tf_norm = _normalize_tf(tf)

    try:
        setter = getattr(global_data, "set_merged_summary", None)
        if callable(setter):
            try:
                setter(tf_norm, df, source="push")
            except TypeError:
                setter(tf_norm, df)
            return
    except Exception:
        logger.exception("[STARTUP] set_merged_summary failed tf=%s", tf_norm)

    try:
        setter = getattr(global_data, "set_push_merged_summary", None)
        if callable(setter):
            setter(tf_norm, df)
            return
    except Exception:
        logger.exception("[STARTUP] set_push_merged_summary failed tf=%s", tf_norm)

    try:
        attr = _legacy_attr_name(tf_norm)
        if attr:
            setattr(global_data, attr, _safe_copy_df(df))
        else:
            setattr(global_data, f"merged_summary_{int(tf_norm)}", _safe_copy_df(df))
    except Exception:
        logger.exception("[STARTUP] merged_summary attr fallback set failed tf=%s", tf_norm)


# ============================================================
# calculation/history summary access
# ============================================================

def get_summary_history_safe(tf: int):
    """
    計算用 summary 履歴 cache を取得する。

    merged summary は表示用に最新1行/銘柄へ圧縮されるため、
    indicator / ranking / scoring が履歴を必要とする場合はこちらを見る。

    優先順位:
      1. global_data.get_summary_history(tf, source="push")
      2. global_data.get_summary_history(tf)
      3. global_data.summary_history_cache[tf]
      4. None
    """
    tf_norm = _normalize_tf(tf)

    try:
        getter = getattr(global_data, "get_summary_history", None)
        if callable(getter):
            try:
                return getter(tf_norm, source="push")
            except TypeError:
                return getter(tf_norm)
    except Exception:
        logger.exception("[STARTUP] get_summary_history failed tf=%s", tf_norm)

    try:
        cache = getattr(global_data, "summary_history_cache", None)
        if isinstance(cache, dict):
            df = cache.get(tf_norm)
            if df is not None:
                return _safe_copy_df(df)
    except Exception:
        logger.exception("[STARTUP] summary_history_cache fallback failed tf=%s", tf_norm)

    return None


def set_summary_history_safe(tf: int, df) -> None:
    """
    計算用 summary 履歴 cache へ保存する。

    重要:
      ここでは最新1行/銘柄へ圧縮しない。
      DB seed で読み込んだ履歴DFを保持するための保存先。
    """
    tf_norm = _normalize_tf(tf)

    try:
        setter = getattr(global_data, "set_summary_history", None)
        if callable(setter):
            try:
                setter(tf_norm, df, source="push")
            except TypeError:
                setter(tf_norm, df)
            return
    except Exception:
        logger.exception("[STARTUP] set_summary_history failed tf=%s", tf_norm)

    try:
        cache = getattr(global_data, "summary_history_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(global_data, "summary_history_cache", cache)

        cache[tf_norm] = _safe_copy_df(df)

        if _is_nonempty_df(df):
            logger.info(
                "[STARTUP] summary_history_cache fallback set tf=%s rows=%d",
                tf_norm,
                len(df),
            )
        else:
            logger.info(
                "[STARTUP] summary_history_cache fallback set tf=%s rows=0",
                tf_norm,
            )

    except Exception:
        logger.exception("[STARTUP] summary_history_cache fallback set failed tf=%s", tf_norm)


# ============================================================
# generic compatibility aliases
# ============================================================

def get_merged_summary_safe(tf: int, source: str = "push"):
    """
    汎用互換 getter。

    source="push" の場合は get_push_merged_summary_safe() に委譲。
    source="ranking" / "legacy" なども global_data.get_merged_summary()
    が対応していれば取得する。
    """
    source = (source or "push").strip().lower()

    if source == "push":
        return get_push_merged_summary_safe(tf)

    tf_norm = _normalize_tf(tf)

    try:
        getter = getattr(global_data, "get_merged_summary", None)
        if callable(getter):
            try:
                return getter(tf_norm, source=source)
            except TypeError:
                return getter(tf_norm)
    except Exception:
        logger.exception(
            "[STARTUP] get_merged_summary_safe failed tf=%s source=%s",
            tf_norm,
            source,
        )

    return None


def set_merged_summary_safe(tf: int, df, source: str = "push") -> None:
    """
    汎用互換 setter。

    source="push" の場合は set_push_merged_summary_safe() に委譲。
    それ以外は global_data.set_merged_summary() が対応していれば保存する。
    """
    source = (source or "push").strip().lower()

    if source == "push":
        set_push_merged_summary_safe(tf, df)
        return

    tf_norm = _normalize_tf(tf)

    try:
        setter = getattr(global_data, "set_merged_summary", None)
        if callable(setter):
            try:
                setter(tf_norm, df, source=source)
            except TypeError:
                setter(tf_norm, df)
            return
    except Exception:
        logger.exception(
            "[STARTUP] set_merged_summary_safe failed tf=%s source=%s",
            tf_norm,
            source,
        )


__all__ = [
    "get_push_merged_summary_safe",
    "set_push_merged_summary_safe",
    "get_summary_history_safe",
    "set_summary_history_safe",
    "get_merged_summary_safe",
    "set_merged_summary_safe",
]