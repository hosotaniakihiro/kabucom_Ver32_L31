# ============================================================
# File   : trading/summary/top_candidates_pkg/utils.py
# Version: Ver2.2-PRODUCTION-SUMMARY-TOP-CANDIDATES-UTILS
# ------------------------------------------------------------
# Function:
#   - 安全な型変換
#   - symbol / side / interval 正規化
#   - score 解決
#   - summary dataframe 妥当性判定
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable, List

import pandas as pd

logger = logging.getLogger(__name__)


def safe_copy_df(df: Any) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df.copy()
    return pd.DataFrame()


def ensure_dataframe(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()

    if isinstance(obj, pd.DataFrame):
        return obj.copy()

    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def safe_symbol(v: Any) -> str:
    """
    symbol を安全に文字列化・正規化する。

    例:
      7203.0 -> 7203
      '7203 JP' -> 7203
    """

    try:
        s = str(v).strip()
    except Exception:
        return ""

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


def safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        if pd.isna(v):
            return default
        return str(v)
    except Exception:
        return default


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default


def ensure_numeric_col(
    df: pd.DataFrame,
    col: str,
    default: float = 0.0,
) -> pd.DataFrame:
    try:
        if col not in df.columns:
            df[col] = default

        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(default)

    except Exception:
        logger.exception("[TOP CANDIDATES] numeric coercion failed col=%s", col)
        try:
            df[col] = default
        except Exception:
            pass

    return df


def normalize_signal(v: Any) -> str:
    try:
        s = str(v).strip().upper()
    except Exception:
        return ""

    if s in ("BUY", "LONG", "B", "買", "買い"):
        return "BUY"

    if s in ("SELL", "SHORT", "S", "売", "売り"):
        return "SELL"

    return ""


def normalize_side(v: Any) -> str:
    s = normalize_signal(v)
    if s:
        return s

    try:
        s = str(v).strip().upper()
    except Exception:
        return ""

    if s in ("BUY", "SELL"):
        return s

    return ""


def normalize_interval(interval: Any) -> str:
    try:
        s = str(interval).strip()

        if s.endswith("min"):
            return s

        if s.endswith("m") and s[:-1].isdigit():
            return f"{int(s[:-1])}min"

        return f"{int(float(s))}min"

    except Exception:
        return str(interval)


def interval_to_int(interval: Any) -> int:
    try:
        s = str(interval).strip().lower()
        s = s.replace("min", "").replace("m", "")
        return int(float(s))
    except Exception:
        return 0


def tf_candidates(interval: int) -> List[Any]:
    """
    GlobalContext の _normalize_tf に合わせた tf 候補。
    """

    interval = int(interval)

    return [
        interval,
        str(interval),
        f"{interval}m",
        f"{interval}min",
    ]


def side_score_column(side: str) -> str:
    side = normalize_side(side)

    if side == "SELL":
        return "score_sell"

    return "score_buy"


def main_score(row: pd.Series, side: str) -> float:
    """
    AI候補選別用の主スコア。

    BUY:
      score_buy -> final_score -> display_score -> score_total -> score

    SELL:
      score_sell -> final_score -> display_score -> score_total -> score
    """

    side = normalize_side(side) or "BUY"
    side_col = side_score_column(side)

    for col in [side_col, "final_score", "display_score", "score_total", "score"]:
        if col in row.index:
            val = safe_float(row.get(col), default=0.0)
            if val != 0.0:
                return val

    return 0.0


def first_existing_value(
    row: pd.Series,
    cols: Iterable[str],
    default: Any = None,
) -> Any:
    for col in cols:
        if col in row.index:
            val = row.get(col)

            if val not in (None, ""):
                try:
                    if pd.isna(val):
                        continue
                except Exception:
                    pass

                return val

    return default


def has_meaningful_value(v: Any) -> bool:
    if v is None:
        return False

    if isinstance(v, str):
        return v.strip() != ""

    try:
        if pd.isna(v):
            return False
    except Exception:
        pass

    if isinstance(v, (int, float)):
        return float(v) != 0.0

    return True


def is_completed_summary_like(df: pd.DataFrame) -> bool:
    """
    AI候補抽出に使える最低限の summary 判定。

    GlobalContext 側にも completed 判定はあるが、
    ここでは top_candidates 側で安全に再確認する。
    """

    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False

        if "symbol" not in df.columns:
            return False

        symbol_ok = df["symbol"].fillna("").astype(str).str.strip().ne("").any()
        if not symbol_ok:
            return False

        score_cols = [
            c for c in [
                "score",
                "score_buy",
                "score_sell",
                "final_score",
                "display_score",
                "score_total",
            ]
            if c in df.columns
        ]

        if not score_cols:
            return False

        for col in score_cols:
            s = pd.to_numeric(df[col], errors="coerce")
            if s.notna().sum() > 0:
                return True

        return False

    except Exception:
        return False