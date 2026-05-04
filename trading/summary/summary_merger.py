# ============================================================
# summary_merger.py
# Ver26.4.2-FINAL-COMPAT-READONLY-MERGED-SAFE
# ------------------------------------------------------------
# ✔ Ver26.4.1 の全API・思想を保持（削除ゼロ）
# ✔ GlobalData Ver26+ 完全互換
# ✔ merged_summary を唯一の正として READ-ONLY 利用
# ✔ base/live/merge API は互換維持用ラッパー
# ✔ Scheduler SAFE（例外非伝播）
# ============================================================

import logging
import pandas as pd
from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# base_summary（互換API・READ ONLY）
# ============================================================
def get_base_summary(interval: int) -> pd.DataFrame:
    """
    互換API:
    旧設計では Yahoo DB 正本だったが、
    現在は GlobalData.merged_summary が唯一の正。

    → 単に merged を返す（READ ONLY）
    """
    try:
        df = global_data.get_summary(interval)
    except Exception:
        logger.exception("[summary_merger] get_summary failed interval=%s", interval)
        return pd.DataFrame()

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    df = df.copy()

    if "datetime" not in df.columns:
        logger.error("[summary_merger] merged summary has no datetime column")
        return pd.DataFrame()

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)

    return df.sort_values("datetime").reset_index(drop=True)


# ============================================================
# live_summary（互換API・NO-OP）
# ============================================================
def get_live_summary_1min() -> pd.DataFrame:
    """
    互換API:
    live_summary は scheduled_summary で既に merged 済み。

    → 新規生成はしない（安全に空を返す）
    """
    logger.debug(
        "[summary_merger] get_live_summary_1min is deprecated → empty"
    )
    return pd.DataFrame()


# ============================================================
# base + live 統合（互換API・READ ONLY）
# ============================================================
def merge_base_and_live(interval: int) -> pd.DataFrame:
    # baseデータ（Yahoo）を最優先で読み込む
    df_base = get_base_summary(interval)
    df_live = get_live_summary_1min()

    if df_base.empty and df_live.empty:
        logger.warning(f"[SUMMARY] both base and live are empty for {interval}min")
        return pd.DataFrame()

    if df_base.empty:
        return df_live

    if df_live.empty:
        return df_base

    # datetime の重複を解消する
    last_base_dt = df_base["datetime"].max()
    df_live_new = df_live[df_live["datetime"] > last_base_dt]

    # baseデータとliveデータの統合（datetime順で並べる）
    df_merged = pd.concat([df_base, df_live_new], ignore_index=True).sort_values("datetime")
    df_merged = df_merged.drop_duplicates(subset=["symbol", "datetime"], keep="first")

    return df_merged


