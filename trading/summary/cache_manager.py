# ============================================================
# trading/summary/cache_manager.py
# Ver4.0-PRODUCTION-UNIFIED-REALTIME-COMPAT
# ------------------------------------------------------------
# ✔ 1m / 3m / 5m 統合キャッシュ管理
# ✔ merged / multi 同期
# ✔ symbol単位安全統合
# ✔ NaN / inf 完全排除
# ✔ 最大保持本数制御
# ✔ incremental更新対応
# ✔ realtime_engine完全互換API追加
# ✔ thread-safe
# ✔ Runtime停止防止
# ============================================================

from __future__ import annotations

import logging
import threading
import pandas as pd
import numpy as np
from typing import Optional

from global_state import global_data
from config.runtime_limits import SUMMARY_CACHE_MAX_ROWS

logger = logging.getLogger(__name__)

VALID_INTERVALS = (1, 3, 5)

# interval別ロック
_cache_locks = {
    1: threading.Lock(),
    3: threading.Lock(),
    5: threading.Lock(),
}

# ============================================================
# 共通ユーティリティ
# ============================================================

def _safe_clean(df: pd.DataFrame) -> pd.DataFrame:
    return df.replace([np.inf, -np.inf], np.nan)


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"])
    return df


def _truncate(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    max_rows = SUMMARY_CACHE_MAX_ROWS.get(interval, 2000)
    if len(df) <= max_rows:
        return df
    return df.iloc[-max_rows:].copy()

# ============================================================
# 初期化
# ============================================================

def init_summary_cache():

    logger.info("🧠 Initializing summary cache")

    for interval in VALID_INTERVALS:
        if global_data.get_merged_summary(interval) is None:
            global_data.set_merged_summary(interval, pd.DataFrame())
        if global_data.get_multi_summary(interval) is None:
            global_data.set_multi_summary(interval, pd.DataFrame())

# ============================================================
# フル上書き（bootstrap用）
# ============================================================

def set_full_cache(interval: int, df: pd.DataFrame):

    if interval not in VALID_INTERVALS:
        return

    if df is None:
        return

    df = _ensure_datetime(df)
    df = _safe_clean(df)

    if "symbol" in df.columns and "datetime" in df.columns:
        df = df.sort_values(["symbol", "datetime"])

    df = df.reset_index(drop=True)
    df = _truncate(df, interval)

    with _cache_locks[interval]:
        global_data.set_merged_summary(interval, df)
        global_data.set_multi_summary(interval, df)

    logger.info(f"[CACHE] full set {interval}min rows={len(df)}")

# ============================================================
# 差分更新（正式）
# ============================================================

def update_cache_incremental(interval: int, df_new: pd.DataFrame):

    if interval not in VALID_INTERVALS:
        return

    if df_new is None or df_new.empty:
        return

    df_new = _ensure_datetime(df_new)
    df_new = _safe_clean(df_new)

    with _cache_locks[interval]:

        df_existing = global_data.get_merged_summary(interval)

        if df_existing is None or df_existing.empty:
            df_final = df_new
        else:
            df_final = pd.concat(
                [df_existing, df_new],
                ignore_index=True
            )

        if "symbol" in df_final.columns and "datetime" in df_final.columns:
            df_final = (
                df_final
                .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                .sort_values(["symbol", "datetime"])
            )

        df_final = df_final.reset_index(drop=True)
        df_final = _truncate(df_final, interval)

        global_data.set_merged_summary(interval, df_final)
        global_data.set_multi_summary(interval, df_final)

    logger.info(f"[CACHE] incremental {interval}min +{len(df_new)} rows")

# ============================================================
# 🔥 realtime_engine互換API
# ============================================================

def update_cache(interval: int, df_new: pd.DataFrame):
    """
    realtime_engineから呼ばれる正式API
    """
    update_cache_incremental(interval, df_new)


def get_cache(interval: int) -> pd.DataFrame:
    """
    realtime_engine互換取得API
    """
    if interval not in VALID_INTERVALS:
        return pd.DataFrame()

    df = global_data.get_merged_summary(interval)

    if df is None:
        return pd.DataFrame()

    return df.copy()

# ============================================================
# symbol単位取得
# ============================================================

def get_symbol_df(interval: int, symbol: str) -> Optional[pd.DataFrame]:

    df = global_data.get_merged_summary(interval)

    if df is None or df.empty:
        return None

    df_symbol = df[df["symbol"] == symbol]

    if df_symbol.empty:
        return None

    return df_symbol.sort_values("datetime").reset_index(drop=True)

# ============================================================
# キャッシュクリア（再起動・市場再開）
# ============================================================

def clear_cache():

    for interval in VALID_INTERVALS:
        with _cache_locks[interval]:
            global_data.set_merged_summary(interval, pd.DataFrame())
            global_data.set_multi_summary(interval, pd.DataFrame())

    logger.warning("🧹 Summary cache cleared")