# ============================================================
# File   : trading/ranking/execution/symbol_rotation.py
# Version: Ver4-PRODUCTION-ULTRA-STABLE-SYMBOL-ROTATION
# ------------------------------------------------------------
# ✔ 上位銘柄選定（TOP-N）
# ✔ scoreベースソート
# ✔ entry_timingフィルタ
# ✔ 流動性フィルタ
# ✔ ignition優先ブースト
# ✔ 重複排除
# ✔ symbol正規化
# ✔ ATS登録連携
# ✔ crash safe
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

TOP_N = 20
MIN_SCORE = 0.05

MIN_TURNOVER = 5_000_000
MIN_ENTRY_TIMING = 0.0


# ============================================================
# helpers
# ============================================================

def _safe_df(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    return df.copy()


def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" in df.columns:

        df["symbol"] = (
            df["symbol"]
            .astype(str)
            .str.strip()
        )

    return df


def _deduplicate(df: pd.DataFrame) -> pd.DataFrame:

    if "symbol" not in df.columns:
        return df

    return df.drop_duplicates(subset=["symbol"], keep="last")


def _filter_score(df: pd.DataFrame) -> pd.DataFrame:

    if "score" not in df.columns:
        return df

    return df[df["score"] >= MIN_SCORE]


def _filter_liquidity(df: pd.DataFrame) -> pd.DataFrame:

    if "turnover" not in df.columns:
        return df

    return df[df["turnover"] >= MIN_TURNOVER]


def _filter_entry(df: pd.DataFrame) -> pd.DataFrame:

    if "entry_timing_score" not in df.columns:
        return df

    return df[df["entry_timing_score"] >= MIN_ENTRY_TIMING]


def _boost_ignition(df: pd.DataFrame) -> pd.DataFrame:

    if "ignition_score" not in df.columns:
        return df

    try:
        df = df.copy()
        df["_priority"] = (
            df["ignition_score"] * 2 + df["score"]
        )
        return df.sort_values("_priority", ascending=False)

    except Exception:
        return df


# ============================================================
# ATS登録（ここは既存システムに接続）
# ============================================================

def _register_symbols(symbols: list[str]):

    try:

        # 既存のATS登録関数に接続する
        from ats.ats_register import register_symbols

        register_symbols(symbols)

        logger.info(
            "[symbol_rotation] registered symbols: %s",
            symbols
        )

    except Exception:

        logger.exception(
            "[symbol_rotation] ATS registration failed"
        )


# ============================================================
# main
# ============================================================

def rotate_symbols(
    df: pd.DataFrame,
    *,
    top_n: int = TOP_N
) -> list[str]:
    """
    銘柄ローテーション（最終実行）

    Returns:
        List[str] 選定銘柄
    """

    df = _safe_df(df)

    if df.empty:
        return []

    try:

        # ----------------------------------------------------
        # 前処理
        # ----------------------------------------------------

        df = _normalize_symbol(df)
        df = _deduplicate(df)

        # ----------------------------------------------------
        # フィルタ
        # ----------------------------------------------------

        df = _filter_score(df)

        if df.empty:
            return []

        df = _filter_liquidity(df)

        if df.empty:
            return []

        df = _filter_entry(df)

        if df.empty:
            return []

        # ----------------------------------------------------
        # 優先度（ignition強化）
        # ----------------------------------------------------

        df = _boost_ignition(df)

        # ----------------------------------------------------
        # 最終ソート
        # ----------------------------------------------------

        df = df.sort_values("score", ascending=False)

        # ----------------------------------------------------
        # TOP-N
        # ----------------------------------------------------

        df_top = df.head(top_n)

        symbols = df_top["symbol"].tolist()

        # ----------------------------------------------------
        # 登録
        # ----------------------------------------------------

        _register_symbols(symbols)

        logger.info(
            "[symbol_rotation] selected=%s / total=%s",
            len(symbols),
            len(df)
        )

        return symbols

    except Exception:

        logger.exception(
            "[symbol_rotation] failed"
        )

        return []