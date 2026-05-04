# ============================================================
# trading/summary/summary_rebuilder.py
# （Ver24-FINAL-CORRECT）
# ------------------------------------------------------------
# ✔ summary_saver の実装に合わせる
# ✔ save_summary_1min 等は一切使わない
# ✔ upsert_summary(interval, df) を使用
# ============================================================

import logging
import pandas as pd

from global_state import global_data
from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.scoring import calc_buy_score, calc_sell_score

# ★ 実在する関数（ログから確定）
from trading.summary.summary_saver import upsert_summary

logger = logging.getLogger(__name__)


def rebuild_and_update(df_1m_new: pd.DataFrame) -> pd.DataFrame:

    if df_1m_new is None or df_1m_new.empty:
        return pd.DataFrame()

    df = df_1m_new.copy()

    required = {"symbol", "datetime", "close_price"}
    if not required.issubset(df.columns):
        logger.error(f"[rebuild] missing columns: {required - set(df.columns)}")
        return pd.DataFrame()

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    df["datetime"] = df["datetime"].dt.tz_localize(None)

    if df.empty:
        return pd.DataFrame()

    # Indicator
    df = add_all_indicators(df)

    # Score
    (
        df["score_buy"],
        df["buy_reasons"],
        df["buy_reason_scores"],
    ) = calc_buy_score(df, interval=1)

    (
        df["score_sell"],
        df["sell_reasons"],
        df["sell_reason_scores"],
    ) = calc_sell_score(df, interval=1)

    # 最新1本
    df_latest = (
        df.sort_values("datetime")
          .groupby("symbol", as_index=False)
          .tail(1)
          .reset_index(drop=True)
    )

    if df_latest.empty:
        return pd.DataFrame()

    # ★ DB 保存（ここが正解）
    try:
        upsert_summary(df_latest, interval=1)
        logger.info(f"💾 summary saved interval=1 rows={len(df_latest)}")
    except Exception:
        logger.exception("❌ summary upsert failed")

    # Cache 更新
    try:
        df_cache = global_data.get_multi_summary(1)
        if isinstance(df_cache, pd.DataFrame) and not df_cache.empty:
            df_all = pd.concat([df_cache, df_latest], ignore_index=True)
        else:
            df_all = df_latest.copy()

        df_all = (
            df_all.sort_values(["symbol", "datetime"])
                  .drop_duplicates(["symbol", "datetime"], keep="last")
                  .reset_index(drop=True)
        )

        global_data.set_multi_summary(1, df_all)

    except Exception:
        logger.exception("❌ cache update failed")

    return df_latest
