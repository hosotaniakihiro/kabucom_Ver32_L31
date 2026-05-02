# ============================================================
# trading/ai/async_ai_engine.py
# Ver4.3-PRODUCTION-MTF-GUARDED-SCORE-SAVE-FINAL
#     -LAZY-SCORING-IMPORT-FIX
# ------------------------------------------------------------
# ✔ Ver4.2 全機能完全保持（削除ゼロ）
# ✔ scoring_main 非同期実行
# ✔ interval対応
# ✔ 重複実行防止
# ✔ queue爆発防止（連続抑制）
# ✔ graceful shutdown
# ✔ Runtime停止防止
# ✔ 例外完全吸収
# ✔ realtime_engine互換API
# ✔ MTF列保証
# ✔ scoring後DB保存
# ✔ advanced_mtf 警告完全解消
# ✔ FIX: scoring_main の循環 import 回避（遅延 import）
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Optional

import pandas as pd
import numpy as np

from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

MAX_WORKERS = 1
MIN_JOB_INTERVAL = 0.5


# ============================================================
# Lazy import helper
# ============================================================

def _get_scoring_main():
    """
    循環 import 回避のため、scoring_main は関数内で遅延 import する。
    """
    from trading.scoring.core.scoring_core import scoring_main
    return scoring_main


# ============================================================
# Async AI Engine
# ============================================================

class AsyncAIEngine:

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._lock = threading.Lock()

        self._last_submit_time = 0.0
        self._current_future: Optional[Future] = None
        self._running = True

    # ========================================================
    # MTF列保証
    # ========================================================

    def _inject_mtf_columns(
        self,
        df: pd.DataFrame,
    ) -> pd.DataFrame:

        if df is None or df.empty:
            return df

        df = df.copy()

        required_cols = [
            "slope_atr_scaled",
            "slope_atr_scaled_3m",
            "slope_atr_scaled_5m",
        ]

        for col in required_cols:
            if col not in df.columns:
                logger.debug(
                    "[AI] auto inject missing MTF column: %s",
                    col,
                )
                df[col] = 0.0

            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], 0.0)
                .fillna(0.0)
                .astype("float64")
            )

        return df

    # ========================================================
    # AIジョブ投入
    # ========================================================

    def submit_ai_job(
        self,
        df: pd.DataFrame,
        interval: int = 1,
    ):
        if not self._running:
            return

        if df is None or df.empty:
            return

        now = time.time()

        # ----------------------------------------------------
        # 連続発火抑制（CPU暴走防止）
        # ----------------------------------------------------
        if now - self._last_submit_time < MIN_JOB_INTERVAL:
            return

        with self._lock:

            # ------------------------------------------------
            # 前ジョブがまだ動いている場合はスキップ
            # ------------------------------------------------
            if (
                self._current_future is not None
                and not self._current_future.done()
            ):
                return

            try:
                self._current_future = self._executor.submit(
                    self._safe_run_scoring,
                    df.copy(),
                    interval,
                )

                self._last_submit_time = now

            except Exception:
                logger.exception("AI submit failed")

    # ========================================================
    # 安全実行ラッパー（scoring後保存）
    # ========================================================

    def _safe_run_scoring(
        self,
        df: pd.DataFrame,
        interval: int,
    ):
        try:
            logger.debug("[AI] scoring start (%smin)", interval)

            # MTF列保証
            df = self._inject_mtf_columns(df)

            # ------------------------------------------------
            # ① スコア計算
            # ------------------------------------------------
            scoring_main = _get_scoring_main()
            df_scored = scoring_main(df, interval=interval)

            if df_scored is None or df_scored.empty:
                return

            # ------------------------------------------------
            # ② スコア入りでDB保存
            # ------------------------------------------------
            try:
                bulk_upsert_summary(df_scored, interval=interval)
            except TypeError:
                bulk_upsert_summary(df_scored, interval)

            logger.debug("[AI] scoring + save done (%smin)", interval)

        except Exception:
            logger.exception("[AI] scoring crashed")

    # ========================================================
    # 状態確認
    # ========================================================

    def is_busy(self) -> bool:
        if self._current_future is None:
            return False

        return not self._current_future.done()

    # ========================================================
    # 停止
    # ========================================================

    def shutdown(self, wait: bool = True):
        logger.info("🛑 AsyncAIEngine shutting down")

        self._running = False

        try:
            self._executor.shutdown(wait=wait)
        except Exception:
            logger.exception("AI executor shutdown failed")

        logger.info("🛑 AsyncAIEngine stopped")


# ============================================================
# Singleton
# ============================================================

async_ai_engine = AsyncAIEngine()


# ============================================================
# realtime_engine互換API
# ============================================================

def submit_ai_job(
    df: pd.DataFrame,
    interval: int = 1,
):
    """
    realtime_engineから呼ばれる正式API
    """
    async_ai_engine.submit_ai_job(df, interval)