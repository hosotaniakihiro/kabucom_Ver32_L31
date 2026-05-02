# ============================================================
# File   : trading/summary/realtime_summary_diff_engine.py
# Version: 1.0-REALTIME-DIFF-ENGINE-PRODUCTION-STABLE
# ------------------------------------------------------------
# ✔ PUSH差分処理
# ✔ 1分足確定生成
# ✔ 3分 / 5分は必要時のみ更新
# ✔ 再構築しない
# ✔ score破壊しない
# ✔ MA75完全維持
# ✔ DB差分保存のみ
# ✔ GCキャッシュ同期
# ✔ 例外安全
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import datetime as dt

from global_state import global_data
from core.global_context.context import global_context as GC

from trading.summary.confirmed_bar_builder import build_confirmed_1min_from_push
from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from trading.summary.resample import resample_1min_to
from trading.summary.summary_cache_utils import normalize_summary_cache

logger = logging.getLogger(__name__)


# ============================================================
# メインエントリ
# ============================================================

def run_realtime_diff_update() -> bool:
    """
    1分ごとに呼ばれる想定
    PUSH → 1分足差分 → 必要なら3分/5分更新
    """

    try:

        df_push = getattr(global_data, "push_df", None)

        if df_push is None or df_push.empty:
            return True

        df_push = df_push.copy()
        df_push["datetime"] = pd.to_datetime(
            df_push["datetime"], errors="coerce"
        )
        df_push = df_push.dropna(subset=["datetime"])

        # ----------------------------------------------------
        # 現在キャッシュの1分足
        # ----------------------------------------------------
        df_1m_prev = GC.summary._cache.get(1)

        if df_1m_prev is None or df_1m_prev.empty:
            return True

        last_dt = df_1m_prev["datetime"].max()

        df_push_new = df_push[df_push["datetime"] > last_dt]

        if df_push_new.empty:
            return True

        # ----------------------------------------------------
        # 1分足差分生成
        # ----------------------------------------------------
        cutoff = df_push_new["datetime"].max()

        df_new_1m = build_confirmed_1min_from_push(
            df_push_new,
            cutoff,
        )

        if df_new_1m.empty:
            return True

        df_new_1m = add_all_indicators(
            df_new_1m,
            interval="1min",
        )

        # DB保存（差分のみ）
        bulk_upsert_summary(df_new_1m, 1)

        # ----------------------------------------------------
        # キャッシュ更新
        # ----------------------------------------------------
        merged_1m = normalize_summary_cache(
            prev=df_1m_prev,
            new=df_new_1m,
            max_rows=5000,
        )

        GC.summary._cache[1] = merged_1m
        global_data.set_merged_summary(1, merged_1m)
        global_data.set_multi_summary(1, merged_1m)

        # ----------------------------------------------------
        # 3分 / 5分 更新判定
        # ----------------------------------------------------
        _update_higher_timeframes(merged_1m)

        return True

    except Exception:
        logger.exception("❌ realtime diff engine failed")
        return False


# ============================================================
# 上位足更新
# ============================================================

def _update_higher_timeframes(df_1m: pd.DataFrame):

    now = dt.datetime.now()

    # --------------------------------------------------------
    # 3分足
    # --------------------------------------------------------
    if now.minute % 3 == 0:
        _update_interval(df_1m, 3)

    # --------------------------------------------------------
    # 5分足
    # --------------------------------------------------------
    if now.minute % 5 == 0:
        _update_interval(df_1m, 5)


def _update_interval(df_1m: pd.DataFrame, interval: int):

    try:

        df_prev = GC.summary._cache.get(interval)

        df_new = resample_1min_to(df_1m, interval)

        if df_new is None or df_new.empty:
            return

        df_new = add_all_indicators(
            df_new,
            interval=f"{interval}min",
        )

        bulk_upsert_summary(df_new, interval)

        merged = normalize_summary_cache(
            prev=df_prev,
            new=df_new,
            max_rows=2000,
        )

        GC.summary._cache[interval] = merged
        global_data.set_merged_summary(interval, merged)
        global_data.set_multi_summary(interval, merged)

        logger.info(
            "⏱ %smin updated rows=%d",
            interval,
            len(merged),
        )

    except Exception:
        logger.exception("❌ interval update failed")