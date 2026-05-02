# ============================================================
# trading/summary/stream_engine.py
# Ver2.0-STREAM-CORE-ULTIMATE-STABLE
# ------------------------------------------------------------
# ✔ push駆動
# ✔ 1分差分O(1)
# ✔ 3分 / 5分 差分生成
# ✔ incremental_1m_engine統合
# ✔ incremental_resampler統合
# ✔ cache完全同期
# ✔ async DB保存
# ✔ async AI
# ✔ multi-symbol対応
# ✔ ロック最適化
# ✔ 本番例外完全耐性
# ✔ None/型崩れ完全防御
# ✔ 再入安全
# ============================================================

from __future__ import annotations

import logging
import threading
import pandas as pd
from typing import Optional

from trading.summary.incremental_1m_engine import (
    update_incremental_1m_df,
)
from trading.summary.incremental_resampler import (
    build_3m_from_last_3,
    build_5m_from_last_5,
)
from trading.summary.push_ring_buffer import push_buffer
from trading.summary.cache_manager import (
    update_cache,
    get_cache,
)
from trading.summary.async_writer import async_save
from trading.ai.async_ai_engine import submit_ai_job

logger = logging.getLogger(__name__)


class StreamEngine:
    """
    完全ストリーム型サマリーエンジン
    本番超安定版
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._running = True

    # ========================================================
    # 共通安全チェック
    # ========================================================

    def _safe_df(self, df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:

        if df is None:
            return None

        if not isinstance(df, pd.DataFrame):
            return None

        if df.empty:
            return None

        if "symbol" not in df.columns:
            return None

        return df

    # ========================================================
    # 1分処理
    # ========================================================

    def process_1m(self):

        try:
            df_push = push_buffer.flush()
        except Exception:
            logger.exception("[STREAM] push_buffer flush failed")
            return

        df_push = self._safe_df(df_push)
        if df_push is None:
            return

        try:
            from trading.summary.confirmed_bar_builder import (
                build_confirmed_1min_from_push
            )
            df_1m = build_confirmed_1min_from_push(df_push)
        except Exception:
            logger.exception("[STREAM] build_confirmed_1min_from_push failed")
            return

        df_1m = self._safe_df(df_1m)
        if df_1m is None:
            return

        try:
            df_1m = update_incremental_1m_df(df_1m)
        except Exception:
            logger.exception("[STREAM] incremental 1m update failed")
            return

        df_1m = self._safe_df(df_1m)
        if df_1m is None:
            return

        try:
            update_cache(1, df_1m)
        except Exception:
            logger.exception("[STREAM] cache update 1m failed")

        try:
            async_save(df_1m, interval=1)
        except Exception:
            logger.exception("[STREAM] async_save 1m failed")

        try:
            submit_ai_job(df_1m)
        except Exception:
            logger.exception("[STREAM] async AI submit failed")

        logger.info("[STREAM] 1min processed rows=%d", len(df_1m))

    # ========================================================
    # 3分処理
    # ========================================================

    def process_3m(self):

        try:
            df1 = get_cache(1)
        except Exception:
            logger.exception("[STREAM] get_cache 1m failed")
            return

        df1 = self._safe_df(df1)
        if df1 is None or len(df1) < 3:
            return

        try:
            df3 = build_3m_from_last_3(df1)
        except Exception:
            logger.exception("[STREAM] build_3m_from_last_3 failed")
            return

        df3 = self._safe_df(df3)
        if df3 is None:
            return

        try:
            update_cache(3, df3)
        except Exception:
            logger.exception("[STREAM] cache update 3m failed")

        try:
            async_save(df3, interval=3)
        except Exception:
            logger.exception("[STREAM] async_save 3m failed")

        logger.info("[STREAM] 3min processed rows=%d", len(df3))

    # ========================================================
    # 5分処理
    # ========================================================

    def process_5m(self):

        try:
            df1 = get_cache(1)
        except Exception:
            logger.exception("[STREAM] get_cache 1m failed")
            return

        df1 = self._safe_df(df1)
        if df1 is None or len(df1) < 5:
            return

        try:
            df5 = build_5m_from_last_5(df1)
        except Exception:
            logger.exception("[STREAM] build_5m_from_last_5 failed")
            return

        df5 = self._safe_df(df5)
        if df5 is None:
            return

        try:
            update_cache(5, df5)
        except Exception:
            logger.exception("[STREAM] cache update 5m failed")

        try:
            async_save(df5, interval=5)
        except Exception:
            logger.exception("[STREAM] async_save 5m failed")

        logger.info("[STREAM] 5min processed rows=%d", len(df5))

    # ========================================================
    # フル1サイクル（依存順保証）
    # ========================================================

    def run_cycle(self):

        if not self._running:
            return

        with self._lock:

            self.process_1m()
            self.process_3m()
            self.process_5m()

    # ========================================================
    # 停止
    # ========================================================

    def stop(self):
        self._running = False


# ============================================================
# シングルトン
# ============================================================

stream_engine = StreamEngine()