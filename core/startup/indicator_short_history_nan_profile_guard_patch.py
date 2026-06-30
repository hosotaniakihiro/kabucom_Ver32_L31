# ============================================================
# File   : core/startup/indicator_short_history_nan_profile_guard_patch.py
# Version: V1-IND-SHORT-PROFILE-NAN-GUARD
# ------------------------------------------------------------
# Purpose:
#   trading.summary.pipeline.indicator_short_history_patch._profile で
#   symbol_hist_len が全NaNのとき int(NaN) になり、
#   ValueError: cannot convert float NaN to integer が出る問題を防ぐ。
#
# 背景:
#   PUSH raw DB fallback は起動直後に 1 slot だけのDataFrameを返すことがある。
#   その時点では symbol_hist_len が存在しない/全NaNになりやすく、
#   ログ用profile計算だけで落ちる。
# ============================================================
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-IND-SHORT-PROFILE-NAN-GUARD"
_INSTALLED = False


def _safe_int_max(series: Any, default: int = 0) -> int:
    try:
        s = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
        if not isinstance(s, pd.Series):
            return int(default)
        s = s.dropna()
        if s.empty:
            return int(default)
        v = s.max()
        if pd.isna(v):
            return int(default)
        return int(v)
    except Exception:
        return int(default)


def _safe_bool_sum(value: Any, index) -> int:
    try:
        if isinstance(value, pd.Series):
            return int(value.reindex(index).fillna(False).astype(bool).sum())
        return int(pd.Series(value, index=index).fillna(False).astype(bool).sum())
    except Exception:
        return 0


def _safe_nonnull(df: pd.DataFrame, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or col not in df.columns:
            return 0
        return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        return 0


def _safe_nonzero(df: pd.DataFrame, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return int((s != 0).sum())
    except Exception:
        return 0


def _profile_safe(df: pd.DataFrame) -> dict[str, int]:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return {
                "rows": 0,
                "symbols": 0,
                "hist_max": 0,
                "technical_ready": 0,
                "usable_ready": 0,
                "slope_nonnull": 0,
                "slope_nonzero": 0,
                "score_slope_nonzero": 0,
                "atr_nonnull": 0,
                "rsi_nonnull": 0,
                "macd_nonnull": 0,
                "macd_nonzero": 0,
                "signal_nonnull": 0,
            }
        idx = df.index
        hist_src = df["symbol_hist_len"] if "symbol_hist_len" in df.columns else pd.Series(dtype=float)
        return {
            "rows": int(len(df)),
            "symbols": int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0,
            "hist_max": _safe_int_max(hist_src, 0),
            "technical_ready": _safe_bool_sum(df.get("technical_ready", False), idx),
            "usable_ready": _safe_bool_sum(df.get("usable_technical_ready", False), idx),
            "slope_nonnull": _safe_nonnull(df, "slope"),
            "slope_nonzero": _safe_nonzero(df, "slope"),
            "score_slope_nonzero": _safe_nonzero(df, "score_slope"),
            "atr_nonnull": _safe_nonnull(df, "atr"),
            "rsi_nonnull": _safe_nonnull(df, "rsi"),
            "macd_nonnull": _safe_nonnull(df, "macd"),
            "macd_nonzero": _safe_nonzero(df, "macd"),
            "signal_nonnull": _safe_nonnull(df, "signal"),
        }
    except Exception:
        logger.debug("[IND SHORT PROFILE GUARD] safe profile fallback failed", exc_info=True)
        return {"rows": int(len(df)) if isinstance(df, pd.DataFrame) else 0, "symbols": 0, "hist_max": 0}


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.summary.pipeline.indicator_short_history_patch as target
        old = getattr(target, "_profile", None)
        if getattr(old, "_ind_short_profile_nan_guard_v1", False):
            _INSTALLED = True
            return True
        _profile_safe._ind_short_profile_nan_guard_v1 = True  # type: ignore[attr-defined]
        _profile_safe._original = old  # type: ignore[attr-defined]
        target._profile = _profile_safe
        _INSTALLED = True
        logger.warning("[IND SHORT PROFILE GUARD] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[IND SHORT PROFILE GUARD] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[IND SHORT PROFILE GUARD] auto install failed")

__all__ = ["install", "VERSION"]
