"""
============================================================
history_cache.py
Incremental1MEngine History Cache
------------------------------------------------------------
✔ indicator履歴キャッシュ
✔ DBアクセス削減
✔ 最大400バー保持
✔ symbol別管理
✔ duplicate除去
✔ datetimeソート
✔ メモリ安全
✔ 本番安定版
============================================================
"""

from __future__ import annotations

import pandas as pd
import logging

logger = logging.getLogger(__name__)


class HistoryCache:
    """
    indicator計算用の履歴キャッシュ
    """

    def __init__(self, max_bars: int = 400):

        self.cache = {}

        self.max_bars = max_bars

    # ========================================================
    # GET HISTORY
    # ========================================================

    def get(self, symbol: str):

        try:

            return self.cache.get(symbol)

        except Exception:

            logger.exception(
                "[HistoryCache] get failed"
            )

            return None

    # ========================================================
    # SET HISTORY
    # ========================================================

    def set(self, symbol: str, df: pd.DataFrame):

        try:

            if df is None or df.empty:

                self.cache[symbol] = pd.DataFrame()

                return

            df = (
                df
                .drop_duplicates(
                    subset=["symbol", "datetime"],
                    keep="last"
                )
                .sort_values("datetime")
                .tail(self.max_bars)
                .reset_index(drop=True)
            )

            self.cache[symbol] = df

        except Exception:

            logger.exception(
                "[HistoryCache] set failed"
            )

    # ========================================================
    # APPEND HISTORY
    # ========================================================

    def append(self, symbol: str, df_new: pd.DataFrame):

        try:

            if df_new is None or df_new.empty:
                return

            hist = self.cache.get(symbol)

            # ------------------------------------------------
            # 初期化
            # ------------------------------------------------

            if hist is None or hist.empty:

                df_new = (
                    df_new
                    .drop_duplicates(
                        subset=["symbol", "datetime"],
                        keep="last"
                    )
                    .sort_values("datetime")
                    .tail(self.max_bars)
                    .reset_index(drop=True)
                )

                self.cache[symbol] = df_new

                return

            # ------------------------------------------------
            # concat
            # ------------------------------------------------

            df_all = pd.concat(
                [hist, df_new],
                ignore_index=True
            )

            # ------------------------------------------------
            # duplicate削除
            # ------------------------------------------------

            df_all = df_all.drop_duplicates(
                subset=["symbol", "datetime"],
                keep="last"
            )

            # ------------------------------------------------
            # sort
            # ------------------------------------------------

            df_all = df_all.sort_values(
                "datetime"
            )

            # ------------------------------------------------
            # max bars制限
            # ------------------------------------------------

            df_all = df_all.tail(
                self.max_bars
            )

            df_all = df_all.reset_index(
                drop=True
            )

            self.cache[symbol] = df_all

        except Exception:

            logger.exception(
                "[HistoryCache] append failed"
            )

    # ========================================================
    # CLEAR SYMBOL
    # ========================================================

    def clear_symbol(self, symbol: str):

        try:

            if symbol in self.cache:

                del self.cache[symbol]

        except Exception:

            logger.exception(
                "[HistoryCache] clear_symbol failed"
            )

    # ========================================================
    # CLEAR ALL
    # ========================================================

    def clear(self):

        try:

            self.cache.clear()

        except Exception:

            logger.exception(
                "[HistoryCache] clear failed"
            )

    # ========================================================
    # SIZE
    # ========================================================

    def size(self):

        try:

            return len(self.cache)

        except Exception:

            return 0