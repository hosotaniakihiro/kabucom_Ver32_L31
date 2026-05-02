# ============================================================
# File   : core/startup/merged_summary_access.py
# Version: REV1.0-STARTUP-MERGED-SUMMARY-ACCESS
# ------------------------------------------------------------
# ✔ startup から merged summary access を分離
# ✔ global_data の source="push" 互換アクセス
# ✔ 旧 global_state / 旧 setter/getter にも後方互換
# ============================================================

from __future__ import annotations

import logging

from global_state import global_data

logger = logging.getLogger(__name__)


def get_push_merged_summary_safe(tf: int):
    try:
        getter = getattr(global_data, "get_merged_summary", None)
        if callable(getter):
            try:
                return getter(int(tf), source="push")
            except TypeError:
                return getter(int(tf))
    except Exception:
        logger.exception("[STARTUP] get_merged_summary failed tf=%s", tf)

    try:
        getter = getattr(global_data, "get_push_merged_summary", None)
        if callable(getter):
            return getter(int(tf))
    except Exception:
        logger.exception("[STARTUP] get_push_merged_summary failed tf=%s", tf)

    try:
        return getattr(global_data, f"merged_summary_{int(tf)}", None)
    except Exception:
        logger.exception("[STARTUP] merged_summary attr fallback failed tf=%s", tf)
        return None


def set_push_merged_summary_safe(tf: int, df) -> None:
    try:
        setter = getattr(global_data, "set_merged_summary", None)
        if callable(setter):
            try:
                setter(int(tf), df, source="push")
            except TypeError:
                setter(int(tf), df)
            return
    except Exception:
        logger.exception("[STARTUP] set_merged_summary failed tf=%s", tf)

    try:
        setter = getattr(global_data, "set_push_merged_summary", None)
        if callable(setter):
            setter(int(tf), df)
            return
    except Exception:
        logger.exception("[STARTUP] set_push_merged_summary failed tf=%s", tf)

    try:
        setattr(global_data, f"merged_summary_{int(tf)}", df)
    except Exception:
        logger.exception("[STARTUP] merged_summary attr fallback set failed tf=%s", tf)