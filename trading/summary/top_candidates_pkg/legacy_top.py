# ============================================================
# File   : trading/summary/top_candidates_pkg/legacy_top.py
# Version: Ver2.2-PRODUCTION-SUMMARY-TOP-CANDIDATES-LEGACY-TOP
# ------------------------------------------------------------
# Function:
#   - 既存互換 API
#   - summary DataFrame から BUY TOP / SELL TOP を共通抽出
# ------------------------------------------------------------
# Public APIs:
#   ✔ prepare_buy_sell_top_df()
#   ✔ prepare_buy_top_df()
#   ✔ prepare_sell_top_df()
# ============================================================

from __future__ import annotations

import logging
from typing import Tuple

import pandas as pd

from .utils import (
    safe_copy_df,
    safe_symbol,
    ensure_numeric_col,
    normalize_signal,
)

logger = logging.getLogger(__name__)


def prepare_buy_sell_top_df(
    df: pd.DataFrame,
    buy_top_n: int = 10,
    sell_top_n: int = 10,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    summary DataFrame から BUY TOP / SELL TOP を作る共通関数。

    Existing compatible API.

    優先ルール:
      - BUY  : score_buy 降順
      - score_buy がなければ score を fallback
      - SELL : score_sell 降順
      - score_sell がなければ 0
      - symbol 重複は keep first
      - signal 列を BUY / SELL に補完
    """

    try:
        work = safe_copy_df(df)

        if work.empty:
            logger.info("[TOP CANDIDATES] input empty")
            return pd.DataFrame(), pd.DataFrame()

        if "symbol" not in work.columns:
            logger.warning("[TOP CANDIDATES] symbol column missing cols=%s", list(work.columns))
            return pd.DataFrame(), pd.DataFrame()

        work["symbol"] = work["symbol"].map(safe_symbol)
        work = work[work["symbol"] != ""].copy()

        if work.empty:
            logger.info("[TOP CANDIDATES] no valid symbols")
            return pd.DataFrame(), pd.DataFrame()

        # score_buy fallback
        if "score_buy" not in work.columns:
            if "score" in work.columns:
                work["score_buy"] = work["score"]
            elif "display_score" in work.columns:
                work["score_buy"] = work["display_score"]
            elif "final_score" in work.columns:
                work["score_buy"] = work["final_score"]
            else:
                work["score_buy"] = 0.0

        # score_sell fallback
        if "score_sell" not in work.columns:
            work["score_sell"] = 0.0

        work = ensure_numeric_col(work, "score_buy", 0.0)
        work = ensure_numeric_col(work, "score_sell", 0.0)

        if "signal" not in work.columns:
            work["signal"] = ""

        work["signal"] = work["signal"].map(normalize_signal)

        # BUY TOP
        buy_df = (
            work[work["score_buy"] > 0]
            .sort_values(
                by=["score_buy", "score_sell", "symbol"],
                ascending=[False, True, True],
                kind="mergesort",
            )
            .drop_duplicates(subset=["symbol"], keep="first")
            .head(int(buy_top_n))
            .copy()
        )

        if not buy_df.empty:
            buy_df["signal"] = "BUY"

        # SELL TOP
        sell_df = (
            work[work["score_sell"] > 0]
            .sort_values(
                by=["score_sell", "score_buy", "symbol"],
                ascending=[False, True, True],
                kind="mergesort",
            )
            .drop_duplicates(subset=["symbol"], keep="first")
            .head(int(sell_top_n))
            .copy()
        )

        if not sell_df.empty:
            sell_df["signal"] = "SELL"

        logger.info(
            "[TOP CANDIDATES] buy_rows=%d sell_rows=%d buy_symbols=%s sell_symbols=%s",
            len(buy_df),
            len(sell_df),
            buy_df["symbol"].astype(str).tolist() if not buy_df.empty else [],
            sell_df["symbol"].astype(str).tolist() if not sell_df.empty else [],
        )

        return buy_df, sell_df

    except Exception:
        logger.exception("[TOP CANDIDATES] prepare failed")
        return pd.DataFrame(), pd.DataFrame()


def prepare_buy_top_df(
    df: pd.DataFrame,
    top_n: int = 10,
) -> pd.DataFrame:
    """
    Existing compatible API.
    summary DataFrame から BUY TOP のみ返す。
    """

    buy_df, _ = prepare_buy_sell_top_df(
        df,
        buy_top_n=top_n,
        sell_top_n=top_n,
    )

    return buy_df


def prepare_sell_top_df(
    df: pd.DataFrame,
    top_n: int = 10,
) -> pd.DataFrame:
    """
    Existing compatible API.
    summary DataFrame から SELL TOP のみ返す。
    """

    _, sell_df = prepare_buy_sell_top_df(
        df,
        buy_top_n=top_n,
        sell_top_n=top_n,
    )

    return sell_df