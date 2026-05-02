# ============================================================
# File   : trading/push_summary/pipeline.py
# Version: Ver31_L23-PUSH-SUMMARY-PIPELINE-WRAPPER
# ------------------------------------------------------------
# 機能:
#   - PUSH由来サマリー計算の入口ラッパ
#   - 既存 legacy summary pipeline を包む
#   - 将来の完全分離に向けた入口統一
#
# 目的:
#   - 外部からは run_push_summary_pipeline() だけを呼ばせる
#   - 既存実装を壊さずに PUSH専用経路を作る
#
# 主な関数:
#   - run_push_summary_pipeline(interval=1, **kwargs)
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


def run_push_summary_pipeline(interval: int | str = 1, **kwargs) -> pd.DataFrame:
    """
    PUSH由来サマリー計算の入口
    初期段階では legacy summary pipeline を利用する
    """
    try:
        from trading.summary.pipeline.summary_pipeline import run_summary_pipeline

        df = run_summary_pipeline(interval=interval, **kwargs)
        if isinstance(df, pd.DataFrame):
            logger.info(
                "[push_summary.pipeline] legacy pipeline ok interval=%r rows=%s",
                interval,
                len(df),
            )
            return df

        logger.warning(
            "[push_summary.pipeline] legacy pipeline returned non-DataFrame interval=%r",
            interval,
        )
        return pd.DataFrame()

    except Exception:
        logger.exception(
            "[push_summary.pipeline] run_push_summary_pipeline failed interval=%r",
            interval,
        )
        return pd.DataFrame()