# ==========================================================
# File   : trading/logger/timeframe_logger.py
# Version: Ver1.0-PRODUCTION-TIMEFRAME-CLOSE-LOGGER
# ----------------------------------------------------------
# ✔ 3分足 / 5分足 確定ログ
# ✔ 重複ログ防止
# ✔ symbolname表示対応
# ✔ DataFrame / list 両対応
# ✔ NaN / inf 防御
# ✔ 本番クラッシュ完全防止
# ✔ realtime / scheduler 両対応
# ==========================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ==========================================================
# 内部状態
# ==========================================================

_last_logged_bar = {}


# ==========================================================
# safe number
# ==========================================================

def _num(v, default=0):

    try:

        v = pd.to_numeric(v, errors="coerce")

        if pd.isna(v):
            return default

        return float(v)

    except Exception:

        return default


# ==========================================================
# symbol 表示
# ==========================================================

def _symbol_display(row):

    try:

        symbol = str(row.get("symbol", ""))

        name = row.get("symbolname", symbol)

        if name is None:
            name = symbol

        name = str(name).strip()

        if name == "" or name == "nan":
            name = symbol

        return f"{symbol}({name})"

    except Exception:

        return "UNKNOWN"


# ==========================================================
# TF確定ログ
# ==========================================================

def log_tf_close(interval: int, df):

    """
    3分足 / 5分足などの確定ログ

    Parameters
    ----------
    interval : int
        timeframe (3 / 5 etc)

    df : DataFrame
        summary dataframe
    """

    try:

        if df is None:
            return

        if isinstance(df, pd.DataFrame) is False:
            return

        if df.empty:
            return

        # --------------------------------------
        # datetime取得
        # --------------------------------------

        if "datetime" not in df.columns:
            return

        last_bar = str(df["datetime"].max())

        # --------------------------------------
        # 重複ログ防止
        # --------------------------------------

        prev = _last_logged_bar.get(interval)

        if prev == last_bar:
            return

        _last_logged_bar[interval] = last_bar

        # --------------------------------------
        # ログ開始
        # --------------------------------------

        logger.info(
            "⏱ %s分足確定  bar=%s rows=%s",
            interval,
            last_bar,
            len(df),
        )

        # --------------------------------------
        # TOP銘柄表示
        # --------------------------------------

        df_view = df.copy()

        if "score" in df_view.columns:

            df_view = df_view.sort_values(
                "score",
                ascending=False
            )

        elif "priority_score" in df_view.columns:

            df_view = df_view.sort_values(
                "priority_score",
                ascending=False
            )

        df_view = df_view.head(3)

        for i, (_, row) in enumerate(df_view.iterrows(), 1):

            symbol_disp = _symbol_display(row)

            close = _num(row.get("close", 0))
            volume = _num(row.get("volume", 0))
            rsi = _num(row.get("rsi", 0))

            logger.info(
                "   %s. %s C=%.1f V=%s RSI=%.1f",
                i,
                symbol_disp,
                close,
                int(volume),
                rsi
            )

    except Exception:

        logger.exception("[timeframe_logger] failed")


# ==========================================================
# public API
# ==========================================================

def log_timeframe_close(interval: int, df):

    """
    外部呼び出し用API
    """

    try:

        log_tf_close(interval, df)

    except Exception:

        logger.exception("log_timeframe_close failed")