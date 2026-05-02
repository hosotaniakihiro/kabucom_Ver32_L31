# ============================================================
# File   : trading/summary/summary_incremental_rebuild.py
# Created: 2026-01-21
# Ver    : 1.3.0-FINAL-SUMMARY-CACHE-NORMALIZED-RUNTIME-LIMITS
# ------------------------------------------------------------
# ✔ summary DB の最終時刻以降のみ差分生成
# ✔ confirmed 1min（end_time 確定）を正本とする
# ✔ 1min 差分 → 3min / 5min 再構築
# ✔ indicator / scoring 既存ルート完全踏襲
# ✔ bulk_rebuild とは完全分離
# ✔ 未確定足 / 再計算 / 二重生成 完全防止
# ✔ initial_summary_rebuild 後の cache / DB 前提に完全整合
# ✔ summary_cache=DataFrame 契約 完全対応
# ✔ ★ end_time 型混在事故を構造的に根絶
# ✔ ★ summary_cache_utils 経由で RAM 制限付き管理
# ✔ ★ normalize_summary_cache を明示使用（唯一の正）
# ✔ ★ cache 上限は runtime_limits を唯一の正とする
# ✔ return は必ず dict[str, DataFrame]（契約厳守）
# ============================================================

import logging
import datetime as dt
import pandas as pd
from sqlalchemy import text

from global_state import global_data
from database import summary_engine

from trading.summary.confirmed_bar_builder import build_confirmed_1min_from_push
from trading.summary.resample import resample_1min_to
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from trading.summary.summary_bulk_rebuild import _apply_indicators_and_scoring

from trading.summary.summary_cache_utils import (
    # 互換のため残す（直接使用しない）
    normalize_summary_cache,     # ★ 唯一の正
)

# ★ runtime_limits を唯一の上限定義として使用
from config.runtime_limits import SUMMARY_CACHE_MAX_ROWS

logger = logging.getLogger(__name__)

SUMMARY_TABLE_MAP = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}


# ============================================================
# DB: interval 別 最終 summary end_time 取得
# ============================================================
def get_last_summary_time(interval: int):
    table = SUMMARY_TABLE_MAP.get(interval)
    if not table:
        raise ValueError(interval)

    sql = f"SELECT MAX(end_time) AS last_dt FROM {table}"

    with summary_engine.connect() as conn:
        row = conn.execute(text(sql)).fetchone()
        return row[0] if row and row[0] else None


# ============================================================
# 差分 summary rebuild
# ============================================================
def run_incremental_summary_rebuild() -> dict[str, pd.DataFrame]:

    logger.info("🚀 [INCR] summary_incremental_rebuild START")

    # ★ 契約：必ず返す
    result: dict[str, pd.DataFrame] = {
        "1min": pd.DataFrame(),
        "3min": pd.DataFrame(),
        "5min": pd.DataFrame(),
    }

    # ========================================================
    # 0) push_df guard
    # ========================================================
    df_push = getattr(global_data, "push_df", None)

    if df_push is None or df_push.empty or "datetime" not in df_push.columns:
        logger.info("[INCR] push_df empty or invalid → skip")
        return result

    df_push = df_push.copy()
    df_push["datetime"] = pd.to_datetime(df_push["datetime"], errors="coerce")
    df_push = df_push.dropna(subset=["datetime"])

    if df_push.empty:
        return result

    cutoff_time = dt.datetime.now().replace(second=0, microsecond=0)

    # ========================================================
    # ① last 1min end_time
    # ========================================================
    last_dt_1m = get_last_summary_time(1)
    if last_dt_1m:
        df_push = df_push[df_push["datetime"] > last_dt_1m]

    if df_push.empty:
        return result

    # ========================================================
    # ② confirmed 1min
    # ========================================================
    df_1m = build_confirmed_1min_from_push(
        df_push=df_push,
        cutoff_time=cutoff_time,
    )

    if df_1m is None or df_1m.empty or "end_time" not in df_1m.columns:
        return result

    df_1m = df_1m.copy()

    # ★ end_time 絶対正規化
    df_1m["end_time"] = pd.to_datetime(df_1m["end_time"], errors="coerce")
    df_1m = df_1m.dropna(subset=["end_time"])

    if last_dt_1m:
        df_1m = df_1m[df_1m["end_time"] > last_dt_1m]

    if df_1m.empty:
        return result

    df_1m["interval"] = 1
    df_1m["interval_name"] = "1min"

    # indicator / scoring
    df_1m = _apply_indicators_and_scoring(df_1m, interval_min=1)

    # DB upsert
    bulk_upsert_summary(df_1m, interval=1)

    logger.info(
        "[INCR] 1min saved rows=%d last_end_time=%s",
        len(df_1m),
        df_1m["end_time"].max(),
    )

    # ========================================================
    # summary_cache 更新（1min / 正式ルート）
    # ========================================================
    if hasattr(global_data, "summary_cache"):
        global_data.summary_cache[1] = normalize_summary_cache(
            prev=global_data.summary_cache.get(1),
            new=df_1m,
            max_rows=SUMMARY_CACHE_MAX_ROWS[1],
        )

    result["1min"] = df_1m

    # ========================================================
    # ③ 3min / 5min
    # ========================================================
    for interval in (3, 5):

        last_dt = get_last_summary_time(interval)
        df_xm = resample_1min_to(df_1m, interval)

        if df_xm is None or df_xm.empty or "end_time" not in df_xm.columns:
            continue

        df_xm = df_xm.copy()

        # ★ end_time 絶対正規化
        df_xm["end_time"] = pd.to_datetime(df_xm["end_time"], errors="coerce")
        df_xm = df_xm.dropna(subset=["end_time"])

        if last_dt:
            df_xm = df_xm[df_xm["end_time"] > last_dt]

        if df_xm.empty:
            continue

        df_xm["interval"] = interval
        df_xm["interval_name"] = f"{interval}min"

        df_xm = _apply_indicators_and_scoring(df_xm, interval_min=interval)

        bulk_upsert_summary(df_xm, interval)

        logger.info(
            "[INCR] %dmin saved rows=%d last_end_time=%s",
            interval,
            len(df_xm),
            df_xm["end_time"].max(),
        )

        if hasattr(global_data, "summary_cache"):
            global_data.summary_cache[interval] = normalize_summary_cache(
                prev=global_data.summary_cache.get(interval),
                new=df_xm,
                max_rows=SUMMARY_CACHE_MAX_ROWS[interval],
            )

        result[f"{interval}min"] = df_xm

    logger.info(
        "🎉 [INCR] summary_incremental_rebuild END "
        "(1min=%d 3min=%d 5min=%d)",
        len(result["1min"]),
        len(result["3min"]),
        len(result["5min"]),
    )

    return result
