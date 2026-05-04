# ============================================================
# File   : trading/summary/summary_bulk_rebuild.py
# Created: 2025-12-22 20:30 JST
# ------------------------------------------------------------
# ✔ PUSH DB 全履歴 → summary DB 全履歴 一括生成（BULK）
# ✔ initial は summary seed + push 差分のみ再構築
# ✔ confirmed 1min を正本とする
# ✔ MA75 安定用 seed 読み込み対応
# ✔ 1min / 3min / 5min 同一 indicator + scoring パイプライン
# ✔ scoring_main() を唯一の正ルートとする
# ============================================================

import logging
import pandas as pd

from global_state import global_data
from trading.summary.resample import resample_1min_to
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from trading.summary.confirmed_bar_builder import build_confirmed_1min_from_push
from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.scoring.core.scoring_core import scoring_main
from trading.summary.seed_loader import load_seed_summary as load_summary_seed

logger = logging.getLogger(__name__)

# ★ 市場終了時刻（日本株）
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN = 30

# MA75 安定用 seed 本数
SEED_LIMIT = 400


# ============================================================
# 内部：indicator + scoring（唯一の正ルート）
# ============================================================
def _apply_indicators_and_scoring(
    df: pd.DataFrame,
    interval_min: int,
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = add_all_indicators(df, interval=interval_min)
    df = scoring_main(df)

    return df


# ============================================================
# BULK summary rebuild（PUSH 全履歴・完全再構築）
# ============================================================
def run_bulk_summary_rebuild():

    logger.info("🚀 [BULK] summary_bulk_rebuild START")

    df_push = global_data.push_df
    if df_push is None or df_push.empty:
        logger.error("[BULK] push_df empty → abort")
        return

    if "datetime" not in df_push.columns:
        logger.error("[BULK] push_df missing datetime → abort")
        return

    df_push = df_push.copy()
    df_push["datetime"] = pd.to_datetime(df_push["datetime"], errors="coerce")
    df_push = df_push.dropna(subset=["datetime"])

    if df_push.empty:
        logger.error("[BULK] push_df invalid after datetime parse → abort")
        return

    trade_date = df_push["datetime"].dt.normalize().max()
    cutoff = trade_date + pd.Timedelta(
        hours=MARKET_CLOSE_HOUR,
        minutes=MARKET_CLOSE_MIN,
    )

    df_push = df_push[df_push["datetime"] <= cutoff]

    logger.info(
        "[BULK] push_df rows=%d symbols=%d cutoff=%s",
        len(df_push),
        df_push["symbol"].nunique(),
        cutoff,
    )

    # ========================================================
    # confirmed 1min
    # ========================================================
    df_1m = build_confirmed_1min_from_push(
        df_push=df_push,
        cutoff_time=cutoff,
    )

    if df_1m is None or df_1m.empty:
        logger.error("[BULK] confirmed 1min empty → abort")
        return

    df_1m["interval"] = 1
    df_1m["interval_name"] = "1min"

    df_1m = _apply_indicators_and_scoring(df_1m, interval_min=1)
    bulk_upsert_summary(df_1m, interval=1)

    logger.info("[BULK] 1min saved rows=%d", len(df_1m))

    # ========================================================
    # 3min / 5min
    # ========================================================
    for interval in (3, 5):

        df_xm = resample_1min_to(df_1m, interval)
        if df_xm is None or df_xm.empty:
            continue

        df_xm["interval"] = interval
        df_xm["interval_name"] = f"{interval}min"

        df_xm = _apply_indicators_and_scoring(df_xm, interval_min=interval)
        bulk_upsert_summary(df_xm, interval)

        logger.info("[BULK] %dmin saved rows=%d", interval, len(df_xm))

    logger.info("🎉 [BULK] summary_bulk_rebuild END")


# ============================================================
# 起動時 initial summary rebuild（seed + 当日差分）
# ============================================================
def run_initial_summary_rebuild():

    logger.info("✨ initial_summary_rebuild START")

    # --------------------------------------------------------
    # ① seed 読み込み（MA75 安定用）
    # --------------------------------------------------------
    df_seed = load_summary_seed(
        interval=1,
        limit=SEED_LIMIT,
    )

    if df_seed.empty:
        logger.warning("[INIT] seed empty → MA75 may be unstable")

    # --------------------------------------------------------
    # ② 当日 push → confirmed 1min
    # --------------------------------------------------------
    df_push = global_data.push_df
    if df_push is None or df_push.empty:
        logger.warning("[INIT] push_df empty")
        return None

    df_push = df_push.copy()
    df_push["datetime"] = pd.to_datetime(df_push["datetime"], errors="coerce")
    df_push = df_push.dropna(subset=["datetime"])

    trade_date = df_push["datetime"].dt.normalize().max()
    cutoff = trade_date + pd.Timedelta(
        hours=MARKET_CLOSE_HOUR,
        minutes=MARKET_CLOSE_MIN,
    )

    df_push = df_push[df_push["datetime"] <= cutoff]

    df_new_1m = build_confirmed_1min_from_push(
        df_push=df_push,
        cutoff_time=cutoff,
    )

    if df_new_1m is None or df_new_1m.empty:
        logger.warning("[INIT] confirmed 1min empty")
        return None

    df_new_1m["interval"] = 1
    df_new_1m["interval_name"] = "1min"

    # --------------------------------------------------------
    # ③ seed + new concat
    # --------------------------------------------------------
    df_all_1m = pd.concat(
        [df_seed, df_new_1m],
        ignore_index=True,
    )

    df_all_1m = df_all_1m.drop_duplicates(
        subset=["symbol", "datetime"],
        keep="last",
    ).sort_values("datetime")

    # --------------------------------------------------------
    # ④ indicator + scoring
    # --------------------------------------------------------
    df_all_1m = _apply_indicators_and_scoring(
        df_all_1m,
        interval_min=1,
    )

    # --------------------------------------------------------
    # ⑤ 当日分のみ保存
    # --------------------------------------------------------
    df_today_1m = df_all_1m[
        df_all_1m["datetime"].dt.normalize() == trade_date
    ]

    bulk_upsert_summary(df_today_1m, interval=1)

    result = {"1min": df_today_1m}

    # --------------------------------------------------------
    # ⑥ 3min / 5min（1min 正本から）
    # --------------------------------------------------------
    for interval in (3, 5):

        df_xm = resample_1min_to(df_all_1m, interval)
        if df_xm is None or df_xm.empty:
            continue

        df_xm["interval"] = interval
        df_xm["interval_name"] = f"{interval}min"

        df_xm = _apply_indicators_and_scoring(
            df_xm,
            interval_min=interval,
        )

        df_today_xm = df_xm[
            df_xm["datetime"].dt.normalize() == trade_date
        ]

        bulk_upsert_summary(df_today_xm, interval)
        result[f"{interval}min"] = df_today_xm

    logger.info(
        "✨ initial_summary_rebuild END "
        "1min=%d 3min=%d 5min=%d",
        len(result.get("1min", [])),
        len(result.get("3min", [])),
        len(result.get("5min", [])),
    )

    return result
