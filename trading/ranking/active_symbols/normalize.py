# ============================================================
# File   : trading/ranking/active_symbols/normalize.py
# Version: Ver1.0-ACTIVE-SYMBOLS-NORMALIZE
# ============================================================
from __future__ import annotations
import datetime as dt
from typing import Any, Iterable, List, Optional, Set
import pandas as pd


def normalize_symbol(symbol: Any) -> Optional[str]:
    if symbol is None:
        return None
    s = str(symbol).strip().upper()
    if not s:
        return None
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2
    s = s.replace("　", "").replace(" ", "")
    if s in {"NAN", "NONE", "NULL", "-", "0"}:
        return None
    if not (3 <= len(s) <= 5):
        return None
    if not s.isalnum():
        return None
    return s


def dedupe_keep_order(items: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for x in items:
        s = normalize_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def safe_numeric_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([0.0] * len(df), index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(0.0)


def first_existing_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def now() -> dt.datetime:
    return dt.datetime.now()


def today_ymd(at: Optional[dt.datetime] = None) -> str:
    return (at or now()).strftime("%Y%m%d")


def today_date_str(at: Optional[dt.datetime] = None) -> str:
    return (at or now()).strftime("%Y-%m-%d")
