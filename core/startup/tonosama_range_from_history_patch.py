# ============================================================
# File   : core/startup/tonosama_range_from_history_patch.py
# Version: V1-TONOSAMA-RANGE-FROM-HISTORY-STRICT
# ------------------------------------------------------------
# Purpose:
#   Tonosama primary filters use _intrabar_range_pct.  Latest PUSH 1m rows
#   can be a one-tick bar (high == low == close), while raw 3m/5m history
#   still proves real movement.  Repair the movement column from existing
#   3m/5m price-change history before the strict filter runs.
#
# Strict policy:
#   This is not a fail-open.  Rows are repaired only from actual recent
#   history columns already produced by volume_surge.py.  If there is still
#   no movement evidence, the normal intrabar_range_low_flat_alert_guard
#   drops the row.
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-TONOSAMA-RANGE-FROM-HISTORY-STRICT"
_INSTALLED = False
_ORIGINAL = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _to_num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").replace([float("inf"), -float("inf")], pd.NA).fillna(default)


def _best_history_move_pct(x: pd.DataFrame) -> pd.Series:
    vals = []
    for col in (
        "intrabar_range_pct_3m",
        "intrabar_range_pct_5m",
        "range_pct_3m",
        "range_pct_5m",
        "price_change_pct_3m",
        "price_change_pct_5m",
        "prev_3m_last_delta_pct",
        "prev_5m_last_delta_pct",
        "_max_price_change_pct",
    ):
        if col in x.columns:
            vals.append(_to_num(x, col, 0.0).abs())
    if not vals:
        return pd.Series(0.0, index=x.index, dtype="float64")
    m = vals[0]
    for s in vals[1:]:
        m = m.combine(s, max)
    return m.fillna(0.0)


def _patch_frame(x: pd.DataFrame) -> pd.DataFrame:
    if x is None or x.empty:
        return x
    y = x.copy()
    if "_intrabar_range_pct" not in y.columns:
        return y
    cur = _to_num(y, "_intrabar_range_pct", 0.0).abs()
    hist = _best_history_move_pct(y)
    # Existing _intrabar_range_pct is percent units.  price_change_pct_* columns are also percent units.
    needs = cur <= 0.0
    repairable = needs & (hist > 0.0)
    if repairable.any():
        y.loc[repairable, "_intrabar_range_pct"] = hist.loc[repairable]
        try:
            logger.warning(
                "[TONOSAMA RANGE REPAIR] repaired rows=%s version=%s sample=%s",
                int(repairable.sum()),
                VERSION,
                y.loc[repairable, [c for c in ["symbol", "symbolname", "close", "_intrabar_range_pct", "_max_price_change_pct", "price_change_pct_3m", "price_change_pct_5m"] if c in y.columns]].head(12).to_dict("records"),
            )
        except Exception:
            pass
    return y


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    if not _env_bool("TONOSAMA_RANGE_FROM_HISTORY_PATCH_ENABLED", True):
        logger.warning("[TONOSAMA RANGE REPAIR] disabled by env version=%s", VERSION)
        return False
    try:
        from trading.entry.tonosama import runner
        original = getattr(runner, "_ensure_actual_movement_cols", None)
        if not callable(original):
            logger.warning("[TONOSAMA RANGE REPAIR] target missing version=%s", VERSION)
            return False
        if getattr(original, "_tonosama_range_from_history_v1", False):
            _INSTALLED = True
            return True
        _ORIGINAL = original

        def _patched(df: pd.DataFrame, *args: Any, **kwargs: Any) -> pd.DataFrame:
            out = original(df, *args, **kwargs)
            return _patch_frame(out)

        setattr(_patched, "_tonosama_range_from_history_v1", True)
        runner._ensure_actual_movement_cols = _patched
        _INSTALLED = True
        logger.warning("[TONOSAMA RANGE REPAIR] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[TONOSAMA RANGE REPAIR] install failed version=%s", VERSION)
        return False


__all__ = ["install", "VERSION"]
