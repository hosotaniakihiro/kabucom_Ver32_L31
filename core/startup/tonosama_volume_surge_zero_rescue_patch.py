# ============================================================
# File   : core/startup/tonosama_volume_surge_zero_rescue_patch.py
# Version: V1-ZERO-VOLUME-SURGE-RESCUE
# ------------------------------------------------------------
# 目的:
#   TONOSAMA runner で _max_volume_surge_ratio が全銘柄 0.0 になり、
#     [TONOSAMA FILTER DROP] reason=volume_surge_low before=29 after=0
#   で全落ちするケースを救済する。
#
#   2026-05-29 14:18ログでは _latest_volume は十分大きく、_intrabar_range_pct も大きいが、
#   surge計算だけが 0.0 になっていた。
#
# 方針:
#   - runner._num_series() を補助し、col="_max_volume_surge_ratio" の時だけ
#     volume / range / body / mtf の条件を満たす行に、MIN_VOLUME_SURGE_RATIO相当を返す。
#   - 既存の price/range rescue や climax guard は後段で維持。
#   - SUMMARY/RANKINGには影響なし。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_NUM_SERIES = None


def _env_on(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if df is None or df.empty or col not in df.columns:
            return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce").fillna(default).astype(float)
    except Exception:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")


def _patched_num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    s = _ORIG_NUM_SERIES(df, col, default) if callable(_ORIG_NUM_SERIES) else _series(df, col, default)
    try:
        if not _env_on("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_ENABLED", True):
            return s
        if col != "_max_volume_surge_ratio":
            return s
        if df is None or df.empty:
            return s

        try:
            import trading.entry.tonosama.runner as runner
            min_surge = float(getattr(runner, "MIN_VOLUME_SURGE_RATIO", 3.0) or 3.0)
        except Exception:
            min_surge = 3.0

        latest_volume = _series(df, "_latest_volume", 0.0)
        intrabar_range = _series(df, "_intrabar_range_pct", 0.0)
        body_change = _series(df, "_body_change_pct", 0.0)
        mtf = _series(df, "mtf", 0.0)
        score_mtf = _series(df, "score_mtf", 0.0)
        final_score = _series(df, "final_score", 0.0)
        score = _series(df, "score", 0.0)

        min_volume = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_VOLUME", 500000.0)
        min_range = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_RANGE_PCT", 4.0)
        min_body = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_BODY_PCT", 0.0)
        min_abs_score = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_ABS_SCORE", 0.8)
        min_mtf = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_MTF", 1.0)

        volume_ok = latest_volume >= min_volume
        range_ok = intrabar_range >= min_range
        body_ok = body_change >= min_body
        score_ok = final_score.abs().combine(score.abs(), max) >= min_abs_score
        mtf_ok = mtf.abs().combine(score_mtf.abs(), max) >= min_mtf
        rescue = (s <= 0) & volume_ok & range_ok & body_ok & score_ok & mtf_ok

        if rescue.any():
            out = s.copy()
            out.loc[rescue] = min_surge
            try:
                sample_cols = [
                    "symbol", "symbolname", "close", "_latest_volume", "_intrabar_range_pct",
                    "_body_change_pct", "_max_volume_surge_ratio", "score", "final_score", "mtf", "score_mtf",
                ]
                sample = df.loc[rescue, [c for c in sample_cols if c in df.columns]].head(10).to_dict("records")
            except Exception:
                sample = []
            logger.warning(
                "[TONOSAMA VOLUME SURGE ZERO RESCUE] rescued=%s min_surge=%.2f min_volume=%.0f min_range=%.3f min_abs_score=%.3f min_mtf=%.3f sample=%s",
                int(rescue.sum()),
                min_surge,
                min_volume,
                min_range,
                min_abs_score,
                min_mtf,
                sample,
            )
            return out
    except Exception:
        logger.exception("[TONOSAMA VOLUME SURGE ZERO RESCUE] failed -> original series")
    return s


def install() -> bool:
    global _INSTALLED, _ORIG_NUM_SERIES
    if _INSTALLED:
        return True
    try:
        import trading.entry.tonosama.runner as runner
        cur = getattr(runner, "_num_series", None)
        if not callable(cur):
            logger.warning("[TONOSAMA VOLUME SURGE ZERO RESCUE] target missing")
            return False
        if getattr(cur, "_tonosama_volume_surge_zero_rescue_patch", False):
            _INSTALLED = True
            return True
        _ORIG_NUM_SERIES = cur
        _patched_num_series._tonosama_volume_surge_zero_rescue_patch = True  # type: ignore[attr-defined]
        _patched_num_series._original = cur  # type: ignore[attr-defined]
        runner._num_series = _patched_num_series
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA VOLUME SURGE ZERO RESCUE] installed v1 enabled=%s min_volume=%s min_range=%s min_abs_score=%s min_mtf=%s",
            _env_on("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_ENABLED", True),
            os.getenv("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_VOLUME", "500000"),
            os.getenv("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_RANGE_PCT", "4.0"),
            os.getenv("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_ABS_SCORE", "0.8"),
            os.getenv("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_MTF", "1.0"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA VOLUME SURGE ZERO RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA VOLUME SURGE ZERO RESCUE] auto install failed")


__all__ = ["install"]
