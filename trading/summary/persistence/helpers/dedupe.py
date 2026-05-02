# ============================================================
# File   : trading/summary/persistence/helpers/dedupe.py
# Version: Ver1.0-SUMMARY-DEDUPE
# ------------------------------------------------------------
# 保存前重複除去
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .dataframe_utils import _ensure_dataframe

logger = logging.getLogger(__name__)


def _dedupe_before_save(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    key_cols = None
    if int(interval) == 1 and {"symbol", "datetime"}.issubset(out.columns):
        key_cols = ["symbol", "datetime"]
    elif {"symbol", "date", "time_range"}.issubset(out.columns):
        key_cols = ["symbol", "date", "time_range"]
    elif {"symbol", "date", "end_time"}.issubset(out.columns):
        key_cols = ["symbol", "date", "end_time"]
    elif {"symbol", "date", "time"}.issubset(out.columns):
        key_cols = ["symbol", "date", "time"]

    if not key_cols:
        return out

    before = len(out)

    try:
        out = out.dropna(subset=key_cols)
    except Exception:
        logger.debug("[SUMMARY] dropna failed key=%s", key_cols, exc_info=True)

    try:
        out = (
            out.sort_values(key_cols, kind="stable")
            .drop_duplicates(subset=key_cols, keep="last")
            .reset_index(drop=True)
        )
    except Exception:
        logger.debug("[SUMMARY] drop_duplicates failed key=%s", key_cols, exc_info=True)

    removed = before - len(out)
    if removed > 0:
        logger.info("[SUMMARY] dedupe removed=%s interval=%s key=%s", removed, interval, key_cols)

    return out