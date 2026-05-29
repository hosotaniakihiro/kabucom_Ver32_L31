# ============================================================
# File   : core/startup/tonosama_price_change_or_range_patch.py
# Version: V2-PRICE-CHANGE-OR-RANGE-NO-SURGE-REQUIRED
# ------------------------------------------------------------
# 目的:
#   TONOSAMA runner の primary/final filter は
#     abs(_max_price_change_pct) >= MIN_PRICE_CHANGE_PCT
#   を必須にしている。
#
# V2:
#   - volume_surge filter 側は fail-open/警告で通っても、
#     _max_volume_surge_ratio 自体は 0.0 のまま残るケースがある。
#   - V1 は min_surge=3.0 必須だったため、price_change_low_abs 救済が発動しなかった。
#   - 既定で min_surge=0.0 に変更し、レンジ・出来高・終値位置が有効なら
#     price_change_low_abs を通過させて後段guardへ渡す。
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
        if not _env_on("TONOSAMA_PRICE_CHANGE_OR_RANGE_ENABLED", True):
            return s
        if col != "_max_price_change_pct":
            return s
        if df is None or df.empty:
            return s

        try:
            import trading.entry.tonosama.runner as runner
            min_price_change = float(getattr(runner, "MIN_PRICE_CHANGE_PCT", 0.2) or 0.2)
        except Exception:
            min_price_change = 0.2

        range_pct = _series(df, "_intrabar_range_pct", 0.0)
        volume = _series(df, "_latest_volume", 0.0)
        surge = _series(df, "_max_volume_surge_ratio", 0.0)
        close_pos = _series(df, "_close_position_pct", 50.0)
        signed_body = _series(df, "_signed_body_change_pct", 0.0)
        slope = _series(df, "_slope", 0.0)
        score = _series(df, "score", 0.0)
        final_score = _series(df, "final_score", 0.0)

        min_range = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_RANGE_PCT", 3.0)
        min_volume = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_VOLUME", 50000.0)
        min_surge = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE", 0.0)
        min_abs_slope = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_ABS_SLOPE", 0.0)
        min_abs_score = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_ABS_SCORE", 0.0)

        range_ok = range_pct >= min_range
        volume_ok = volume >= min_volume
        surge_ok = surge >= min_surge
        slope_ok = slope.abs() >= min_abs_slope
        score_ok = (score.abs() >= min_abs_score) | (final_score.abs() >= min_abs_score)

        # close_pos条件: 極端に上下へ寄っている候補だけでなく、レンジが十分なら中間でも後段guardへ渡す。
        body_or_position_ok = (signed_body.abs() > 0) | (close_pos >= 20.0) | (close_pos <= 80.0) | range_ok
        rescue = (s.abs() < min_price_change) & range_ok & volume_ok & surge_ok & slope_ok & score_ok & body_or_position_ok

        if rescue.any():
            out = s.copy()
            # signed_bodyが0なら score/final_score の符号を使う。全部0なら正方向で閾値だけ満たす。
            sign_base = signed_body.copy()
            sign_base = sign_base.where(sign_base != 0, score)
            sign_base = sign_base.where(sign_base != 0, final_score)
            sign = sign_base.apply(lambda x: 1.0 if float(x) >= 0 else -1.0)
            out.loc[rescue] = sign.loc[rescue] * min_price_change
            try:
                sample_cols = [
                    "symbol", "symbolname", "close", "_latest_volume", "_intrabar_range_pct",
                    "_max_volume_surge_ratio", "_max_price_change_pct", "_close_position_pct",
                    "_signed_body_change_pct", "score", "final_score",
                ]
                sample = df.loc[rescue, [c for c in sample_cols if c in df.columns]].head(10).to_dict("records")
            except Exception:
                sample = []
            logger.warning(
                "[TONOSAMA PRICE/RANGE RESCUE] rescued=%s min_price_change=%.3f min_range=%.3f min_volume=%.0f min_surge=%.2f min_abs_score=%.3f sample=%s",
                int(rescue.sum()),
                min_price_change,
                min_range,
                min_volume,
                min_surge,
                min_abs_score,
                sample,
            )
            return out
    except Exception:
        logger.exception("[TONOSAMA PRICE/RANGE RESCUE] failed -> original series")
    return s


def install() -> bool:
    global _INSTALLED, _ORIG_NUM_SERIES
    if _INSTALLED:
        return True
    try:
        # V2 default: surge不要。sitecustomize が3.0をsetdefault済みでもここで0へ倒す。
        os.environ["TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE"] = os.environ.get("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE_FORCE", "0.0")

        import trading.entry.tonosama.runner as runner
        cur = getattr(runner, "_num_series", None)
        if not callable(cur):
            logger.warning("[TONOSAMA PRICE/RANGE RESCUE] target missing")
            return False
        if getattr(cur, "_tonosama_price_change_or_range_patch_v2", False):
            _INSTALLED = True
            return True
        _ORIG_NUM_SERIES = cur
        _patched_num_series._tonosama_price_change_or_range_patch = True  # type: ignore[attr-defined]
        _patched_num_series._tonosama_price_change_or_range_patch_v2 = True  # type: ignore[attr-defined]
        _patched_num_series._original = cur  # type: ignore[attr-defined]
        runner._num_series = _patched_num_series
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA PRICE/RANGE RESCUE] installed v2 enabled=%s min_range=%s min_volume=%s min_surge=%s",
            _env_on("TONOSAMA_PRICE_CHANGE_OR_RANGE_ENABLED", True),
            os.getenv("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_RANGE_PCT", "3.0"),
            os.getenv("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_VOLUME", "50000"),
            os.getenv("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE", "0.0"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA PRICE/RANGE RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA PRICE/RANGE RESCUE] auto install failed")


__all__ = ["install"]
