# ============================================================
# File   : trading/push/subscription_manager/ranking_source_premarket.py
# Function:
#   - SBI寄前ランキングCSVから初期登録100銘柄を読む
#   - ランキング_寄前気配上昇率上位YYYYMMDD.csv 上位50
#   - ランキング_寄前気配下落率上位YYYYMMDD.csv 上位50
# ------------------------------------------------------------
# Version: PRODUCTION-REV1.0-RANKING-SOURCE-PREMARKET
# ============================================================

from __future__ import annotations

import logging
import re
from typing import List, Optional

import pandas as pd

from .ranking_source_paths import (
    REGISTER_MAX_SYMBOLS,
    is_existing_file,
    premarket_csv_paths,
    today_ymd,
)
from .ranking_source_retention import append_unique, normalize_symbols

logger = logging.getLogger(__name__)


def _first_col(df: pd.DataFrame, candidates) -> Optional[str]:
    if df is None or df.empty:
        return None

    cols = {str(c).lower(): str(c) for c in df.columns}

    for c in candidates:
        key = str(c).lower()
        if key in cols:
            return cols[key]

    lower_cols = [(str(c).lower(), str(c)) for c in df.columns]
    for c in candidates:
        key = str(c).lower()
        for lc, original in lower_cols:
            if key and key in lc:
                return original

    return None


def _read_csv_flexible(path: str) -> pd.DataFrame:
    if not is_existing_file(path):
        return pd.DataFrame()

    encodings = ("utf-8-sig", "cp932", "shift_jis", "utf-8")

    for enc in encodings:
        try:
            df = pd.read_csv(path, encoding=enc)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue

    for enc in encodings:
        try:
            df = pd.read_csv(path, encoding=enc, header=None)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue

    logger.warning("[SUB MANAGER] premarket csv read failed path=%s", path)
    return pd.DataFrame()


def _extract_symbols_from_csv_df(df: pd.DataFrame, *, top_n: int) -> List[str]:
    if df is None or df.empty:
        return []

    work = df.copy()
    work.columns = [str(c) for c in work.columns]

    symbol_col = _first_col(
        work,
        [
            "symbol",
            "code",
            "銘柄コード",
            "コード",
            "証券コード",
            "StockCode",
            "0",
        ],
    )

    syms: List[str] = []

    if symbol_col and symbol_col in work.columns:
        raw = work[symbol_col].head(top_n * 2).tolist()
        syms = normalize_symbols(raw)
        if syms:
            return syms[:top_n]

    # 列名で取れない場合、全セルから4桁コードを探す
    for _, row in work.head(top_n * 3).iterrows():
        found = ""
        for v in row.tolist():
            text = str(v)
            m = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
            if m:
                found = m.group(1)
                break

        if found:
            syms.append(found)

        if len(normalize_symbols(syms)) >= top_n:
            break

    return normalize_symbols(syms)[:top_n]


def load_premarket_csv_symbols(
    *,
    limit: int = REGISTER_MAX_SYMBOLS,
    ymd: Optional[str] = None,
) -> List[str]:
    """
    寄り付き初期用。
    SBI寄前CSVから上昇50 + 下落50 を返す。
    """
    ymd = ymd or today_ymd()
    gainer_path, loser_path = premarket_csv_paths(ymd)

    g_df = _read_csv_flexible(gainer_path)
    l_df = _read_csv_flexible(loser_path)

    gainers = _extract_symbols_from_csv_df(g_df, top_n=50)
    losers = _extract_symbols_from_csv_df(l_df, top_n=50)

    result: List[str] = []
    append_unique(result, gainers, limit=limit)
    append_unique(result, losers, limit=limit)

    logger.info(
        "[SUB MANAGER] premarket csv symbols loaded ymd=%s gainers=%d losers=%d result=%d gainer_path=%s loser_path=%s",
        ymd,
        len(gainers),
        len(losers),
        len(result),
        gainer_path,
        loser_path,
    )

    return result[:limit]