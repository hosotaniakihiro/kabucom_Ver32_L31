# ============================================================
# File   : trading/summary/recovery/persistence_pkg/symbol_utils.py
# Ver    : PRODUCTION-STABLE-REV9.0-SYMBOL-UTILS
# ------------------------------------------------------------
# 【概要】
#   symbol / symbolname normalization helpers
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .imports import global_data
from .column_utils import pick_text_series

logger = logging.getLogger(__name__)


def resolve_symbolname_series(df: pd.DataFrame) -> pd.Series:
    symbol_s = pick_text_series(df, ["symbol"], default="").astype(str).str.strip()
    symbolname_s = pick_text_series(df, ["symbolname"], default="").astype(str).str.strip()
    name_s = pick_text_series(df, ["name"], default="").astype(str).str.strip()

    out = symbolname_s.copy()
    out = out.mask(out.eq(""), name_s)

    try:
        mp = getattr(global_data, "symbol_name_map", {}) if global_data is not None else {}
        if isinstance(mp, dict) and mp:
            mapped = symbol_s.map(lambda x: str(mp.get(str(x).strip(), "")).strip())
            out = out.mask(out.eq(""), mapped)
    except Exception:
        pass

    out = out.mask(out.eq(""), symbol_s)
    out = out.fillna("").astype(str).str.strip()
    out = out.mask(out.eq(""), symbol_s)
    return out


def normalize_text_aliases_for_db(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    try:
        if "symbolname" not in out.columns:
            out["symbolname"] = resolve_symbolname_series(out)
        else:
            resolved = resolve_symbolname_series(out)
            current = out["symbolname"].fillna("").astype(str).str.strip()
            out["symbolname"] = current.mask(current.eq(""), resolved)
    except Exception:
        logger.exception("[summary.recovery.persistence] symbolname normalize failed")

    return out


__all__ = [
    "resolve_symbolname_series",
    "normalize_text_aliases_for_db",
]