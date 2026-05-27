# ============================================================
# File   : core/startup/indicator_fragmentation_runtime_patch.py
# Version: V1.0-INDICATOR-FRAGMENTATION-WARNING-GUARD
# ------------------------------------------------------------
# 目的:
#   trading.summary.indicators.indicator_calculator で、昼休みなどに
#   3000銘柄超へ指標計算した際の
#     PerformanceWarning: DataFrame is highly fragmented
#   大量出力を抑える。
#
# 方針:
#   - pandas PerformanceWarning を indicator_calculator.py 由来に限定して抑制
#   - add_all_indicators / calculate_indicators / add_indicators の戻り値を copy() し、
#     後続処理に断片化DataFrameを渡さない
#
# 注意:
#   - 根本的な列追加高速化は indicator_calculator.py 本体で別途実施可能
#   - まずログ汚染と後続遅延を止める runtime patch
# ============================================================

from __future__ import annotations

import logging
import warnings
from typing import Any, Callable

import pandas as pd

try:
    from pandas.errors import PerformanceWarning
except Exception:  # pragma: no cover
    PerformanceWarning = Warning  # type: ignore

logger = logging.getLogger(__name__)
_INSTALLED = False


def _wrap_indicator_func(fn: Callable[..., Any], *, name: str) -> Callable[..., Any]:
    if getattr(fn, "_indicator_fragmentation_runtime_patch", False):
        return fn

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=PerformanceWarning,
                message=".*DataFrame is highly fragmented.*",
            )
            out = fn(*args, **kwargs)
        try:
            if isinstance(out, pd.DataFrame) and not out.empty:
                return out.copy()
        except Exception:
            pass
        return out

    _wrapped.__name__ = getattr(fn, "__name__", name)
    _wrapped.__doc__ = getattr(fn, "__doc__", None)
    _wrapped._indicator_fragmentation_runtime_patch = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.summary.indicators.indicator_calculator as mod

        patched = []
        for name in ("add_all_indicators", "calculate_indicators", "add_indicators"):
            fn = getattr(mod, name, None)
            if callable(fn):
                setattr(mod, name, _wrap_indicator_func(fn, name=name))
                patched.append(name)

        warnings.filterwarnings(
            "ignore",
            category=PerformanceWarning,
            message=".*DataFrame is highly fragmented.*",
            module=r".*trading\.summary\.indicators\.indicator_calculator.*",
        )

        _INSTALLED = True
        logger.warning("[IND FRAGMENTATION PATCH] installed patched=%s", patched)
        return True
    except Exception:
        logger.exception("[IND FRAGMENTATION PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[IND FRAGMENTATION PATCH] auto install failed")


__all__ = ["install"]
