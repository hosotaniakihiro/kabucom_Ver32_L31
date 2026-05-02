# ============================================================
# ats_utils.py
# Ver1.0-PRODUCTION-ATS-UTILS
# ------------------------------------------------------------
# ✔ 重複除去（順序保持）
# ✔ リスト安全化
# ✔ 数値安全化
# ✔ 時間判定
# ✔ ログ補助
# ✔ NaN / None 防御
# ✔ 本番例外耐性
# ============================================================

import logging
import datetime as dt
import pandas as pd

from typing import List, Any


logger = logging.getLogger(__name__)


# ============================================================
# 順序維持ユニーク
# ============================================================

def unique_keep_order(seq: List[Any]) -> List[str]:

    try:

        return list(dict.fromkeys([str(s) for s in seq if s]))

    except Exception:

        logger.exception("unique_keep_order failed")
        return []


# ============================================================
# リスト安全化
# ============================================================

def safe_list(obj) -> List:

    if obj is None:
        return []

    if isinstance(obj, list):
        return obj

    try:
        return list(obj)
    except Exception:
        return []


# ============================================================
# 数値安全化
# ============================================================

def safe_numeric(series, default=0):

    try:

        s = pd.to_numeric(series, errors="coerce")

        return s.fillna(default)

    except Exception:

        logger.exception("safe_numeric failed")

        return series


# ============================================================
# DataFrame列保証
# ============================================================

def ensure_column(df, column, default=0):

    if column not in df.columns:
        df[column] = default

    return df


# ============================================================
# マーケット時間
# ============================================================

def is_market_open():

    now = dt.datetime.now().time()

    return (
        dt.time(9, 0) <= now <= dt.time(15, 30)
    )


# ============================================================
# プレオープン
# ============================================================

def is_preopen():

    now = dt.datetime.now().time()

    return now < dt.time(9, 15)


# ============================================================
# 昼休み
# ============================================================

def is_lunch_break():

    now = dt.datetime.now().time()

    return (
        dt.time(11, 30) <= now <= dt.time(12, 30)
    )


# ============================================================
# ATSログ補助
# ============================================================

def log_symbol_count(label: str, symbols: List[str]):

    try:

        logger.info(
            "[ATS] %s count=%d",
            label,
            len(symbols)
        )

    except Exception:

        logger.exception("log_symbol_count failed")


# ============================================================
# 銘柄リスト安全化
# ============================================================

def normalize_symbols(symbols):

    if not symbols:
        return []

    try:

        return [
            str(s)
            for s in symbols
            if s
        ]

    except Exception:

        logger.exception("normalize_symbols failed")

        return []


# ============================================================
# リスト結合
# ============================================================

def merge_symbol_lists(*lists):

    merged = []

    try:

        for lst in lists:

            if not lst:
                continue

            merged.extend(lst)

        return unique_keep_order(merged)

    except Exception:

        logger.exception("merge_symbol_lists failed")

        return []