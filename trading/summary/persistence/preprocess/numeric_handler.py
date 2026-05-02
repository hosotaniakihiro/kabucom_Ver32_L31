# ============================================================
# File   : trading/summary/persistence/preprocess/numeric_handler.py
# Version: Ver1.2-PRODUCTION-NUMERIC-HANDLER-HARDENED-SAFE-ID-COLS
# ------------------------------------------------------------
# ✔ Ver1.1 完全互換
# ✔ inf / -inf → NaN 変換
# ✔ numeric dtype を float に統一
# ✔ object列の自動数値化（成功率しきい値）
# ✔ 文字列混在（","や空白）にも耐性
# ✔ symbol / symbolname / name 系は auto-cast 除外
# ✔ datetime / date / time 系は auto-cast 除外
# ✔ 非破壊（数値列のみ対象）
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# AUTO-CAST EXCLUDE COLUMNS
# ============================================================

_EXACT_EXCLUDE_COLS = {
    "symbol",
    "symbolname",
    "name",
    "datetime",
    "date",
    "time",
    "start_time",
    "end_time",
    "time_range",
    "interval",
    "interval_name",
    "market",
    "market_type",
    "exchange",
    "side",
    "status",
    "source",
    "score_reason",
    "cluster",
}

_PARTIAL_EXCLUDE_KEYWORDS = (
    "symbol",
    "name",
    "datetime",
    "date",
    "time",
    "market",
    "reason",
    "cluster",
    "status",
    "source",
)


# ============================================================
# Utility: 文字列の軽いクリーニング（数値変換前）
# ============================================================

def _clean_numeric_strings(s: pd.Series) -> pd.Series:
    """
    数値っぽい文字列を軽く整形：
    - カンマ除去 "1,234" → "1234"
    - 前後空白除去
    - 空文字はNaNへ
    """
    try:
        s = s.astype(str).str.replace(",", "", regex=False).str.strip()
        s = s.replace(
            {
                "": np.nan,
                "None": np.nan,
                "none": np.nan,
                "nan": np.nan,
                "NaN": np.nan,
                "NULL": np.nan,
                "null": np.nan,
                "NaT": np.nan,
                "nat": np.nan,
            }
        )
        return s
    except Exception:
        return s


def _should_exclude_from_autocast(col: str) -> bool:
    try:
        c = str(col).strip()
        c_low = c.lower()

        if c in _EXACT_EXCLUDE_COLS or c_low in _EXACT_EXCLUDE_COLS:
            return True

        for kw in _PARTIAL_EXCLUDE_KEYWORDS:
            if kw in c_low:
                return True

        return False
    except Exception:
        return False


# ============================================================
# MAIN: NUMERIC NORMALIZATION
# ============================================================

def normalize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """
    数値系の安定化：
    - infをNaNへ
    - 数値列はfloatへ統一
    - object列のうち数値に変換できるものは自動キャスト
    - ただし symbol / symbolname / name / datetime 系は除外
    """

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # ① inf → NaN
    # --------------------------------------------------------
    try:
        inf_mask = df.isin([np.inf, -np.inf])
        if inf_mask.values.any():
            count = int(inf_mask.values.sum())
            df = df.replace([np.inf, -np.inf], np.nan)
            logger.warning("[NUMERIC] replaced inf → NaN (%d cells)", count)
    except Exception:
        logger.exception("[NUMERIC] inf replacement failed")

    # --------------------------------------------------------
    # ② 既存の数値列を取得
    # --------------------------------------------------------
    numeric_cols: List[str] = df.select_dtypes(include=[np.number]).columns.tolist()

    # --------------------------------------------------------
    # ③ object列の自動数値化
    # --------------------------------------------------------
    for col in df.columns:
        if col in numeric_cols:
            continue

        if _should_exclude_from_autocast(col):
            logger.debug("[NUMERIC] auto-cast skipped(excluded) → %s", col)
            continue

        try:
            series = df[col]
        except Exception:
            continue

        if series.dtype == "object":
            try:
                s = _clean_numeric_strings(series)
                converted = pd.to_numeric(s, errors="coerce")

                # 変換成功率
                success_ratio = float(converted.notna().mean())

                # しきい値（80%）以上なら数値列とみなす
                if success_ratio >= 0.80:
                    df[col] = converted
                    numeric_cols.append(col)

                    logger.info(
                        "[NUMERIC] auto-cast → %s (ratio=%.2f)",
                        col,
                        success_ratio,
                    )

            except Exception:
                logger.debug("[NUMERIC] auto-cast failed → %s", col)

    # --------------------------------------------------------
    # ④ floatへ統一
    # --------------------------------------------------------
    for col in numeric_cols:
        try:
            df[col] = df[col].astype(float)
        except Exception:
            logger.warning("[NUMERIC] float cast failed → %s", col)

    return df