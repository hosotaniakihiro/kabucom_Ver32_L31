# ================================================================
# trading/summary/helpers.py
# ================================================================
# trading/summary/helpers.py

import pandas as pd
import logging
from trading.summary.scorer.decision import judge_entry_df, judge_short_df

logger = logging.getLogger(__name__)

# ============================================================
# 🔹 判定結果抽出
# ============================================================
def collect_judged_records(scored_df: pd.DataFrame) -> list[dict]:
    """
    add_total_scores 済みの DataFrame に対して
    judge_entry / judge_short を行い、エントリー候補を dict list に変換
    """
    if scored_df is None or scored_df.empty:
        return []

    records = []
    for _, row in scored_df.iterrows():
        entry_decision = judge_entry(row)
        short_decision = judge_short(row)

        if isinstance(entry_decision, (pd.DataFrame, pd.Series)):
            entry_decision = None
        if isinstance(short_decision, (pd.DataFrame, pd.Series)):
            short_decision = None

        if entry_decision is not None or short_decision is not None:
            rec = row.to_dict()
            rec["entry_decision"] = entry_decision
            rec["short_decision"] = short_decision
            records.append(rec)

    logger.debug(f"[collect_judged_records] {len(records)} 件のシグナル検出")
    return records


# ============================================================
# 🔹 共通ユーティリティ
# ============================================================
def _is_nonempty(df: pd.DataFrame | None) -> bool:
    """DataFrame が存在して中身があるかを判定"""
    return isinstance(df, pd.DataFrame) and not df.empty


def filter_push_after_summary(df_push: pd.DataFrame, df_summary: pd.DataFrame, tf_name: str) -> pd.DataFrame:
    """
    Summaryの最終時刻以降のpushデータを抽出する
    """
    if df_push is None or df_push.empty:
        return pd.DataFrame()
    if df_summary is None or df_summary.empty:
        return df_push.copy()

    if "time" not in df_push.columns:
        return pd.DataFrame()

    last_time = df_summary["time"].max() if "time" in df_summary.columns else None
    if last_time is not None:
        return df_push[df_push["time"] > last_time].copy()
    return df_push.copy()


# ============================================================
# 🔹 time_range → time 変換（高速対応）
# ============================================================
def ensure_time_column(df_new: pd.DataFrame, prev_df: pd.DataFrame | None = None, tf_name: str = "") -> pd.DataFrame:
    """
    time_range → time 列を生成する高速補完関数
    - 初回（prev_dfが空）: 全行変換
    - 差分更新時（prev_dfあり）: 新規 time_range のみ変換
    """
    if df_new is None or df_new.empty:
        return df_new

    if "time" in df_new.columns:
        return df_new

    if "time_range" not in df_new.columns:
        logger.warning(f"⚠️ {tf_name}: 'time_range' が存在しないため time補完スキップ")
        df_new["time"] = None
        return df_new

    # --- 変換関数 ---
    def fast_extract(x: str) -> str:
        """"HH:MM - HH:MM" → "HH:MM"（開始時刻を抽出）"""
        if isinstance(x, str) and len(x) >= 5:
            return x[:5]
        return None

    # --- 差分抽出 ---
    if prev_df is not None and not prev_df.empty and "time_range" in prev_df.columns:
        prev_times = set(prev_df["time_range"].dropna().unique())
        mask_new = ~df_new["time_range"].isin(prev_times)
        df_new["time"] = df_new["time_range"].map(fast_extract)
        logger.info(f"⚡ {tf_name}: time列補完 (新規 {mask_new.sum()}件 / 全{len(df_new)}件)")
    else:
        df_new["time"] = df_new["time_range"].map(fast_extract)
        logger.info(f"🕒 {tf_name}: 初回 time_range→time補完 ({len(df_new)}件)")

    return df_new
