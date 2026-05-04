# ============================================================
# File   : trading/summary/summary_incremental.py
# Created: 2025-12-22 20:35 JST
# ------------------------------------------------------------
# ✔ 差分サマリー再計算（incremental rebuild）
# ✔ 確定1分足ルール統一
# ✔ BULK / FAST / realtime 共通基盤
# ✔ テクニカル指標 + スコアをすべて計算・保存
# ============================================================

import pandas as pd
import logging

from global_state import global_data
from trading.summary.confirmed_bar_builder import (
    build_confirmed_1min_from_push,
)
from trading.summary.resample import resample_1min_to
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from trading.summary.indicators.indicator_calculator import add_all_indicators

# ★ スコア統合（統一入口）
from scoring.add_scores import add_all_scores

logger = logging.getLogger(__name__)


# ============================================================
# 共通 incremental 更新
# ============================================================
def run_incremental_summary(
    df_push: pd.DataFrame,
    cutoff_time,
    start_time=None,   # None = 初期 / BULK / FAST
):
    """
    ✔ start_time=None → 初期 / BULK / FAST
    ✔ start_time指定 → 差分 / 定時 / realtime
    """

    logger.info(
        "[INC] run_incremental_summary START "
        f"start={start_time} cutoff={cutoff_time}"
    )

    if df_push is None or df_push.empty:
        logger.warning("[INC] df_push empty")
        return {}

    # ========================================================
    # symbolname map（global_data 由来）
    # ========================================================
    symbolname_map = getattr(global_data, "symbol_name_map", {}) or {}

    # ========================================================
    # ① PUSH → confirmed 1min
    # ========================================================
    df_1m = build_confirmed_1min_from_push(
        df_push=df_push,
        cutoff_time=cutoff_time,
    )

    if df_1m is None or df_1m.empty:
        logger.warning("[INC] confirmed 1min empty")
        return {}

    # --------------------------------------------------------
    # symbol / symbolname
    # --------------------------------------------------------
    df_1m = df_1m.copy()
    df_1m["symbol"] = df_1m["symbol"].astype(str)
    df_1m["symbolname"] = (
        df_1m["symbol"].map(symbolname_map).fillna("")
    )
    df_1m["interval"] = 1

    # ========================================================
    # ② テクニカル指標
    # ========================================================
    df_1m = add_all_indicators(df_1m)

    # ========================================================
    # ③ スコア計算（★最重要）
    # ========================================================
    df_1m = add_all_scores(df_1m)

    # ========================================================
    # ④ DB 保存（1min）
    # ========================================================
    bulk_upsert_summary(df_1m, interval=1)

    logger.info(
        f"[INC] 1min saved rows={len(df_1m)} "
        f"symbols={df_1m['symbol'].nunique()}"
    )

    result = {
        "1m": df_1m,
    }

    # ========================================================
    # ⑤ 3min / 5min
    # ========================================================
    for interval in (3, 5):
        df_xm = resample_1min_to(df_1m, interval)

        if df_xm is None or df_xm.empty:
            logger.warning(f"[INC] {interval}min empty → skip")
            continue

        df_xm = df_xm.copy()
        df_xm["symbol"] = df_xm["symbol"].astype(str)
        df_xm["symbolname"] = (
            df_xm["symbol"].map(symbolname_map).fillna("")
        )
        df_xm["interval"] = interval

        # --- indicators ---
        df_xm = add_all_indicators(df_xm)

        # --- scores ---
        df_xm = add_all_scores(df_xm)

        # --- save ---
        bulk_upsert_summary(df_xm, interval)

        logger.info(
            f"[INC] {interval}min saved rows={len(df_xm)} "
            f"symbols={df_xm['symbol'].nunique()}"
        )

        result[f"{interval}m"] = df_xm

    logger.info("[INC] run_incremental_summary END")

    return result
