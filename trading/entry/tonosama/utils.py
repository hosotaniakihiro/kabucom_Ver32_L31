# ============================================================
# File   : trading/entry/tonosama/utils.py
# Version: Ver1.0-TONOSAMA-ENTRY-UTILS
# ============================================================
from __future__ import annotations
import math
from typing import Any, Optional
import pandas as pd

def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        try:
            if pd.isna(x):
                return float(default)
        except Exception:
            pass
        return x
    except Exception:
        return float(default)

def normalize_symbol(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s

def first_existing_col(df: pd.DataFrame, names: list[str]) -> Optional[str]:
    for c in names:
        if c in df.columns:
            return c
    return None

def dict_get_any(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d.get(k) is not None:
            return d.get(k)
    lower_map = {str(k).lower(): k for k in d.keys()}
    for k in keys:
        real = lower_map.get(str(k).lower())
        if real is not None and d.get(real) is not None:
            return d.get(real)
    return default
