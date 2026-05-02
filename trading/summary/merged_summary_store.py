"""
============================================================
File: trading/summary/merged_summary_store.py
Ver6.0-ABSOLUTE-FINAL-CENTRAL-MERGED-STABLE
------------------------------------------------------------
✔ merged_summary 中央キャッシュ
✔ interval別管理（1 / 3 / 5）
✔ symbol重複排除
✔ datetime保証
✔ NaN / inf完全防御
✔ 最大保持数制限（メモリ保護）
✔ スレッド安全
✔ post_process_summary統合
✔ 本番永久安定版
============================================================
"""

from __future__ import annotations

import threading
import logging
import pandas as pd
import numpy as np

from trading.summary.summary_post_processor import post_process_summary

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

MAX_ROWS_PER_INTERVAL = 5000   # メモリ保護（必要なら調整）


# ============================================================
# 内部ストア
# ============================================================

_store_lock = threading.Lock()

_merged_store: dict[int, pd.DataFrame] = {
    1: pd.DataFrame(),
    3: pd.DataFrame(),
    5: pd.DataFrame(),
}


# ============================================================
# 数値安全化
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    for col in df.columns:
        if np.issubdtype(df[col].dtype, np.number):
            df[col] = (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], 0)
                .fillna(0)
            )

    return df


# ============================================================
# datetime保証
# ============================================================

def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(
            df["datetime"], errors="coerce"
        )
        df = df.dropna(subset=["datetime"])

    return df


# ============================================================
# 更新
# ============================================================

def update_merged_summary(interval: int, df: pd.DataFrame):

    if interval not in (1, 3, 5):
        logger.warning("[MERGED] invalid interval=%s", interval)
        return

    if df is None or df.empty:
        return

    try:
        df = df.copy()

        df = _ensure_datetime(df)
        df = _sanitize_numeric(df)

        if "symbol" not in df.columns:
            logger.warning("[MERGED] symbol missing")
            return

        df["symbol"] = df["symbol"].astype(str)

        # 🔥 スコア後処理
        df = post_process_summary(df)

        with _store_lock:

            existing = _merged_store.get(interval, pd.DataFrame())

            if existing is None or existing.empty:
                merged = df
            else:
                merged = (
                    pd.concat([existing, df])
                    .drop_duplicates(
                        subset=["symbol", "datetime"],
                        keep="last",
                    )
                    .sort_values("datetime")
                )

            # メモリ制限
            if len(merged) > MAX_ROWS_PER_INTERVAL:
                merged = merged.tail(MAX_ROWS_PER_INTERVAL)

            _merged_store[interval] = merged.reset_index(drop=True)

    except Exception:
        logger.exception("[MERGED] update failed")


# ============================================================
# 取得
# ============================================================

def get_merged_summary(interval: int) -> pd.DataFrame:

    if interval not in (1, 3, 5):
        return pd.DataFrame()

    with _store_lock:
        df = _merged_store.get(interval)

        if df is None:
            return pd.DataFrame()

        return df.copy()


# ============================================================
# 全削除（デバッグ用）
# ============================================================

def clear_merged_summary():

    with _store_lock:
        for k in _merged_store:
            _merged_store[k] = pd.DataFrame()

    logger.info("[MERGED] cleared")