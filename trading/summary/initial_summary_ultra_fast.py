# ============================================================
# File : trading/summary/initial_summary_ultra_fast.py
# Version: 2.0-ULTRA-FAST-DIFF-ENGINE-COMPAT-FINAL
# ------------------------------------------------------------
# ✔ DBを正とする
# ✔ 3分足 / 5分足は再計算しない
# ✔ 1分足は差分のみ更新
# ✔ MA75完全成立
# ✔ 起動10秒以内
# ✔ 既存キャッシュ互換
# ✔ Ver26 seed_loader仕様完全対応（bars使用）
# ✔ run_initial_fast_rebuild 旧API完全互換
# ✔ None / 空DF 完全防御
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from global_state import global_data
from core.global_context.context import global_context as GC

from trading.summary.seed_loader import load_seed_summary
from trading.summary.confirmed_bar_builder import build_confirmed_1min_from_push
from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from trading.summary.summary_cache_utils import initialize_summary_cache

logger = logging.getLogger(__name__)


# ============================================================
# メイン（正式名）
# ============================================================

def run_initial_summary_ultra_fast():

    logger.info("🚀 ULTRA FAST INITIAL START")

    # ========================================================
    # ① DBから既存サマリーを読む（再計算しない）
    # ========================================================

    df_1m = load_seed_summary(interval=1, bars=500)
    df_3m = load_seed_summary(interval=3, bars=500)
    df_5m = load_seed_summary(interval=5, bars=500)

    if df_1m is None:
        df_1m = pd.DataFrame()

    if df_3m is None:
        df_3m = pd.DataFrame()

    if df_5m is None:
        df_5m = pd.DataFrame()

    # ========================================================
    # ② 最新時刻取得
    # ========================================================

    last_1m = (
        df_1m["datetime"].max()
        if not df_1m.empty and "datetime" in df_1m.columns
        else None
    )

    # ========================================================
    # ③ PUSH差分取得
    # ========================================================

    df_push = getattr(global_data, "push_df", None)

    if (
        df_push is None
        or df_push.empty
        or last_1m is None
    ):
        logger.info("⚡ no push diff → DB only mode")
        _sync_cache(df_1m, df_3m, df_5m)
        return True

    df_push = df_push.copy()

    if "datetime" not in df_push.columns:
        logger.warning("⚠ push_df missing datetime")
        _sync_cache(df_1m, df_3m, df_5m)
        return True

    df_push["datetime"] = pd.to_datetime(
        df_push["datetime"],
        errors="coerce"
    )

    df_push = df_push.dropna(subset=["datetime"])
    df_push = df_push[df_push["datetime"] > last_1m]

    if df_push.empty:
        logger.info("⚡ push not newer → skip rebuild")
        _sync_cache(df_1m, df_3m, df_5m)
        return True

    # ========================================================
    # ④ 1分足差分生成
    # ========================================================

    logger.info("⚡ building incremental 1min bars")

    cutoff = df_push["datetime"].max()

    df_new_1m = build_confirmed_1min_from_push(
        df_push,
        cutoff
    )

    if df_new_1m is None or df_new_1m.empty:
        _sync_cache(df_1m, df_3m, df_5m)
        return True

    # テクニカル計算（差分のみ）
    df_new_1m = add_all_indicators(df_new_1m, interval="1min")

    # DB保存
    bulk_upsert_summary(df_new_1m, 1)

    # 既存1分足とマージ
    df_1m = pd.concat([df_1m, df_new_1m], ignore_index=True)

    if not df_1m.empty:
        df_1m = (
            df_1m
            .drop_duplicates(subset=["symbol", "datetime"], keep="last")
            .sort_values(["symbol", "datetime"])
            .reset_index(drop=True)
        )

    # ========================================================
    # ⑤ キャッシュ同期（再計算しない）
    # ========================================================

    _sync_cache(df_1m, df_3m, df_5m)

    logger.info("✅ ULTRA FAST INITIAL COMPLETE")

    return True


# ============================================================
# キャッシュ同期
# ============================================================

def _sync_cache(df_1m, df_3m, df_5m):

    initialize_summary_cache(GC.summary._cache, 1, df_1m)
    initialize_summary_cache(GC.summary._cache, 3, df_3m)
    initialize_summary_cache(GC.summary._cache, 5, df_5m)

    global_data.set_merged_summary(1, df_1m)
    global_data.set_merged_summary(3, df_3m)
    global_data.set_merged_summary(5, df_5m)

    global_data.set_multi_summary(1, df_1m)
    global_data.set_multi_summary(3, df_3m)
    global_data.set_multi_summary(5, df_5m)


# ============================================================
# 旧API互換（summary_loader対策）
# ============================================================

def run_initial_fast_rebuild():
    """
    旧summary_loader互換API
    Ver25以前の呼び出し名に対応
    """
    return run_initial_summary_ultra_fast()


# ============================================================
# exports
# ============================================================

__all__ = [
    "run_initial_summary_ultra_fast",
    "run_initial_fast_rebuild",
]