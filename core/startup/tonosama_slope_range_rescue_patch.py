# ============================================================
# File   : core/startup/tonosama_slope_range_rescue_patch.py
# Version: V1-TONOSAMA-SLOPE-RANGE-RESCUE
# ------------------------------------------------------------
# 目的:
#   TONOSAMA runner の primary/final filter は
#     abs(_slope) >= MIN_SLOPE
#   を必須にしている。
#
#   ただし、PUSH/ランキング由来の1分特徴では slope が非常に小さく、
#   日中レンジ・出来高・終値位置は十分でも slope_abs_too_small で落ちる。
#
# 対策:
#   - runner._num_series(df, "_slope") の戻り値だけ補正。
#   - abs(_slope) < MIN_SLOPE でも、レンジ・出来高が十分なら
#     MIN_SLOPE 相当の値に置き換え、後段guardへ渡す。
#   - 方向は signed_body_change_pct -> score -> final_score -> close位置で推定。
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
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default).astype(float)


def _patched_num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    s = _ORIG_NUM_SERIES(df, col, default) if callable(_ORIG_NUM_SERIES) else _series(df, col, default)
    try:
        if not _env_on("TONOSAMA_SLOPE_RANGE_RESCUE_ENABLED", True):
            return s
        if col != "_slope":
            return s
        if df is None or df.empty:
            return s

        try:
            import trading.entry.tonosama.runner as runner
            min_slope = float(getattr(runner, "MIN_SLOPE", 0.001) or 0.001)
        except Exception:
            min_slope = 0.001

        range_pct = _series(df, "_intrabar_range_pct", 0.0)
        volume = _series(df, "_latest_volume", 0.0)
        close_pos = _series(df, "_close_position_pct", 50.0)
        signed_body = _series(df, "_signed_body_change_pct", 0.0)
        score = _series(df, "score", 0.0)
        final_score = _series(df, "final_score", 0.0)
        price_chg = _series(df, "_max_price_change_pct", 0.0)
        surge = _series(df, "_max_volume_surge_ratio", 0.0)

        min_range = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_RANGE_PCT", 4.0)
        min_volume = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_VOLUME", 500000.0)
        min_abs_score = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_ABS_SCORE", 0.0)
        min_surge = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_SURGE", 0.0)
        close_pos_low = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_CLOSE_POS_LOW", 15.0)
        close_pos_high = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_CLOSE_POS_HIGH", 85.0)

        range_ok = range_pct >= min_range
        volume_ok = volume >= min_volume
        score_ok = (score.abs() >= min_abs_score) | (final_score.abs() >= min_abs_score)
        surge_ok = surge >= min_surge
        close_pos_ok = (close_pos <= close_pos_low) | (close_pos >= close_pos_high) | range_ok

        rescue = (s.abs() < min_slope) & range_ok & volume_ok & score_ok & surge_ok & close_pos_ok
        if rescue.any():
            out = s.copy()
            sign_base = signed_body.copy()
            sign_base = sign_base.where(sign_base != 0, price_chg)
            sign_base = sign_base.where(sign_base != 0, score)
            sign_base = sign_base.where(sign_base != 0, final_score)
            # close_pos が高値圏ならBUY方向、安値圏ならSELL方向の仮符号。
            sign_base = sign_base.where(sign_base != 0, close_pos - 50.0)
            sign = sign_base.apply(lambda x: 1.0 if float(x) >= 0 else -1.0)
            out.loc[rescue] = sign.loc[rescue] * min_slope
            try:
                sample_cols = [
                    "symbol", "symbolname", "close", "_latest_volume", "_intrabar_range_pct",
                    "_max_volume_surge_ratio", "_max_price_change_pct", "_slope",
                    "_close_position_pct", "_signed_body_change_pct", "score", "final_score",
                ]
                sample = df.loc[rescue, [c for c in sample_cols if c in df.columns]].head(10).to_dict("records")
            except Exception:
                sample = []
            logger.warning(
                "[TONOSAMA SLOPE/RANGE RESCUE] rescued=%s min_slope=%.6f min_range=%.3f min_volume=%.0f min_surge=%.2f min_abs_score=%.3f sample=%s",
                int(rescue.sum()),
                min_slope,
                min_range,
                min_volume,
                min_surge,
                min_abs_score,
                sample,
            )
            return out
    except Exception:
        logger.exception("[TONOSAMA SLOPE/RANGE RESCUE] failed -> original series")
    return s


def install() -> bool:
    global _INSTALLED, _ORIG_NUM_SERIES
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_ENABLED", "1")
        os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_RANGE_PCT", "4.0")
        os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_VOLUME", "500000")
        os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_SURGE", "0.0")
        os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_ABS_SCORE", "0.0")

        import trading.entry.tonosama.runner as runner
        cur = getattr(runner, "_num_series", None)
        if not callable(cur):
            logger.warning("[TONOSAMA SLOPE/RANGE RESCUE] target missing")
            return False
        if getattr(cur, "_tonosama_slope_range_rescue_patch", False):
            _INSTALLED = True
            return True
        _ORIG_NUM_SERIES = cur
        _patched_num_series._tonosama_slope_range_rescue_patch = True  # type: ignore[attr-defined]
        _patched_num_series._original = cur  # type: ignore[attr-defined]
        runner._num_series = _patched_num_series
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA SLOPE/RANGE RESCUE] installed v1 enabled=%s min_range=%s min_volume=%s min_surge=%s",
            os.getenv("TONOSAMA_SLOPE_RANGE_RESCUE_ENABLED", "1"),
            os.getenv("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_RANGE_PCT", "4.0"),
            os.getenv("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_VOLUME", "500000"),
            os.getenv("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_SURGE", "0.0"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA SLOPE/RANGE RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA SLOPE/RANGE RESCUE] auto install failed")


__all__ = ["install"]
