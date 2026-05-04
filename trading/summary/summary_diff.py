# ============================================================
# summary_diff.py（Ver1.0）
# ------------------------------------------------------------
# interval（1/3/5分）ごとの summary DF の差分（新規バー）だけ抽出する。
#   - 新規 time_range の行だけ返す
#   - global_data.merged_summary に保存済みのものは除外
# ============================================================

import pandas as pd
import logging
from global_state import global_data

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# interval (1/3/5min) の差分抽出
# ------------------------------------------------------------
def get_summary_diff(new_df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    new_df（今回生成した summary）
      - symbol
      - date
      - time_range
      をキーにして、
    global_data.merged_summary[interval] に存在しないレコードだけ返す。

    戻り値：差分DF（UPSERT対象の新規行）
    """
    if new_df is None or new_df.empty:
        logger.info(f"[summary_diff] interval={interval} new_df 空 → 差分なし")
        return pd.DataFrame()

    # ---- すでに global_data にある summary ----
    old_df = global_data.get_merged_summary(interval)
    if old_df is None or old_df.empty:
        logger.info(f"[summary_diff] interval={interval} old_df 空 → 全件が新規")
        return new_df

    # ---- キー列 ----
    key_cols = ["symbol", "date", "time_range"]

    new_df2 = new_df.copy()
    old_df2 = old_df[key_cols].drop_duplicates()

    # ---- merge で新規キーを抽出 ----
    merged = new_df2.merge(
        old_df2,
        on=key_cols,
        how="left",
        indicator=True,
    )

    diff_df = merged[merged["_merge"] == "left_only"].drop(columns=["_merge"])

    if diff_df.empty:
        logger.info(f"[summary_diff] interval={interval} → 新規バーなし")
        return pd.DataFrame()

    logger.info(f"[summary_diff] interval={interval} → 新規 {len(diff_df)} 行")
    return diff_df.reset_index(drop=True)
