# ============================================================
# File   : trading/history/history_manager.py
# Version: Ver1.0-PRODUCTION-HISTORY-MANAGER
# ------------------------------------------------------------
# ✔ symbol別履歴保持
# ✔ rolling指標用履歴確保
# ✔ 最大履歴制御
# ✔ duplicate完全除去
# ✔ datetime整列
# ✔ NaN耐性
# ✔ global_data連携
# ✔ incremental更新対応
# ✔ メモリ安全
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

MAX_HISTORY_ROWS = 500     # symbolごと履歴
HISTORY_KEY = "summary_history_1m"


# ============================================================
# 初期化
# ============================================================

def initialize_history():

    if not hasattr(global_data, HISTORY_KEY):

        setattr(global_data, HISTORY_KEY, pd.DataFrame())

        logger.info("[HISTORY] initialized")


# ============================================================
# 履歴取得
# ============================================================

def get_history() -> pd.DataFrame:

    initialize_history()

    return getattr(global_data, HISTORY_KEY)


# ============================================================
# 履歴保存
# ============================================================

def set_history(df: pd.DataFrame):

    setattr(global_data, HISTORY_KEY, df)


# ============================================================
# 新バー追加
# ============================================================

def append_bar(new_bar: pd.DataFrame) -> pd.DataFrame:
    """
    new_bar:
        symbol datetime open high low close volume ...
    """

    if new_bar is None or new_bar.empty:
        return get_history()

    history = get_history()

    try:

        df = pd.concat([history, new_bar], ignore_index=True)

        # datetime整列
        if "datetime" in df.columns:
            df = df.sort_values(["symbol", "datetime"])

        # 重複削除
        if {"symbol", "datetime"}.issubset(df.columns):

            df = df.drop_duplicates(
                subset=["symbol", "datetime"],
                keep="last"
            )

        # symbolごと履歴制御
        df = df.groupby("symbol").tail(MAX_HISTORY_ROWS)

        df = df.reset_index(drop=True)

        set_history(df)

        return df

    except Exception as e:

        logger.exception("[HISTORY] append_bar failed")

        return history


# ============================================================
# 最新バー取得
# ============================================================

def get_latest_rows(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        latest = df.groupby("symbol").tail(1)

        return latest.reset_index(drop=True)

    except Exception:

        logger.exception("[HISTORY] latest extraction failed")

        return df


# ============================================================
# symbol履歴取得
# ============================================================

def get_symbol_history(symbol: str) -> pd.DataFrame:

    history = get_history()

    if history.empty:
        return history

    try:

        df = history[history["symbol"] == symbol]

        return df.reset_index(drop=True)

    except Exception:

        logger.exception("[HISTORY] symbol history failed")

        return pd.DataFrame()


# ============================================================
# 履歴サイズログ
# ============================================================

def log_history_status():

    history = get_history()

    if history.empty:
        logger.info("[HISTORY] empty")
        return

    try:

        symbols = history["symbol"].nunique()

        rows = len(history)

        logger.info(
            f"[HISTORY] rows={rows} symbols={symbols}"
        )

    except Exception:

        logger.exception("[HISTORY] log failed")


# ============================================================
# 履歴クリア
# ============================================================

def clear_history():

    set_history(pd.DataFrame())

    logger.warning("[HISTORY] cleared")