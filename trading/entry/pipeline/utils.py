# ============================================================
# File   : trading/entry/pipeline/utils.py
# Function:
#   - entry pipeline 共通 utility
#   - 安全な型変換
#   - symbol / side / source 正規化
#   - score 解決
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-UTILS
# ============================================================

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from .constants import (
    SOURCE_SUMMARY,
    SOURCE_RANKING,
    SOURCE_AI,
    SOURCE_COMBINED,
)


def safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        if pd.isna(v):
            return ""
        return str(v).strip()
    except Exception:
        return ""


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default


def safe_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v

    if v is None:
        return default

    try:
        s = str(v).strip().lower()
    except Exception:
        return default

    if s in ("1", "true", "t", "yes", "y", "on"):
        return True

    if s in ("0", "false", "f", "no", "n", "off", ""):
        return False

    return default


def safe_symbol(v: Any) -> str:
    s = safe_str(v)

    if not s:
        return ""

    if s.lower() in ("nan", "none", "null"):
        return ""

    if "." in s:
        left, right = s.split(".", 1)
        if right.strip("0") == "":
            s = left.strip()
        else:
            s = left.strip()

    if " " in s:
        parts = [p for p in s.split() if p]
        if parts:
            s = parts[0]

    return s.strip()


def safe_copy_df(df: Any) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df.copy()
    return pd.DataFrame()


def normalize_side(v: Any) -> Optional[str]:
    s = safe_str(v).upper()

    if s in ("BUY", "LONG", "B", "買", "買い"):
        return "BUY"

    if s in ("SELL", "SHORT", "S", "売", "売り"):
        return "SELL"

    return None


def normalize_source(v: Any) -> str:
    s = safe_str(v).lower()

    if s in ("", "none", "null"):
        return ""

    if s in ("summary", "push", "push_summary", "pushed_summary"):
        return SOURCE_SUMMARY

    if s in ("ranking", "rank", "ranking_summary"):
        return SOURCE_RANKING

    if s in ("ai", "ai_entry"):
        return SOURCE_AI

    if s in ("combined", "all", "both", "ai_candidates", "summary_ai", "entry"):
        return SOURCE_COMBINED

    return s


def interval_label_to_int(v: Any, default: int = 0) -> int:
    try:
        s = str(v).strip().lower()
        s = s.replace("min", "").replace("m", "")
        return int(float(s))
    except Exception:
        return default


def candidate_score(item: dict) -> float:
    if not isinstance(item, dict):
        return 0.0

    for key in (
        "ai_priority_score",
        "combined_entry_score",
        "entry_score",
        "score",
        "final_score",
        "display_score",
        "score_buy",
        "score_sell",
    ):
        val = safe_float(item.get(key), 0.0)
        if val != 0.0:
            return val

    return 0.0


def first_non_empty(*values: Any) -> Any:
    for v in values:
        if v is None:
            continue

        if isinstance(v, str) and not v.strip():
            continue

        try:
            if pd.isna(v):
                continue
        except Exception:
            pass

        return v

    return None