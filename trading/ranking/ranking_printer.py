# ============================================================
# trading/ranking/ranking_printer.py
# ------------------------------------------------------------
# ランキング表示専用（差分・新規ランクイン）
# ------------------------------------------------------------
# ✔ 表示・ログ専用（判断・AI・DB操作なし）
# ✔ rank_diff / is_new を分かりやすく可視化
# ✔ job / scheduler / デバッグ から安全に呼べる
# ============================================================

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
def print_ranking_diff(
    df: pd.DataFrame,
    *,
    top_n: int = 10,
    title: str | None = None,
):
    """
    ランキング差分をログ表示

    Args:
        df (pd.DataFrame):
            ranking_diff_update 済み DataFrame
            必須列:
              - symbol
              - rank
              - rank_diff
              - is_new
        top_n (int):
            表示最大件数
        title (str | None):
            ログタイトル（任意）
    """

    if df is None or df.empty:
        logger.info("📊 ranking diff: empty")
        return

    if title:
        logger.info("📊 %s", title)

    # --------------------------------------------------------
    # 急上昇ランキング
    # --------------------------------------------------------
    if "rank_diff" in df.columns:
        df_up = (
            df[df["rank_diff"].notna() & (df["rank_diff"] > 0)]
            .sort_values("rank_diff", ascending=False)
            .head(top_n)
        )

        if not df_up.empty:
            logger.info("🚀 急上昇ランキング TOP%d", top_n)
            for _, r in df_up.iterrows():
                logger.info(
                    "⬆ %s  rank=%s  diff=+%d",
                    r.get("symbol"),
                    r.get("rank"),
                    int(r.get("rank_diff")),
                )

    # --------------------------------------------------------
    # 急降下ランキング
    # --------------------------------------------------------
    if "rank_diff" in df.columns:
        df_down = (
            df[df["rank_diff"].notna() & (df["rank_diff"] < 0)]
            .sort_values("rank_diff")
            .head(top_n)
        )

        if not df_down.empty:
            logger.info("⬇ 急降下ランキング TOP%d", top_n)
            for _, r in df_down.iterrows():
                logger.info(
                    "⬇ %s  rank=%s  diff=%d",
                    r.get("symbol"),
                    r.get("rank"),
                    int(r.get("rank_diff")),
                )

    # --------------------------------------------------------
    # 新規ランクイン
    # --------------------------------------------------------
    if "is_new" in df.columns:
        df_new = df[df["is_new"]].head(top_n)

        if not df_new.empty:
            logger.info("🆕 新規ランクイン TOP%d", top_n)
            for _, r in df_new.iterrows():
                logger.info(
                    "🆕 %s  rank=%s",
                    r.get("symbol"),
                    r.get("rank"),
                )


# ============================================================
def print_ranking_summary(
    ranking_map: dict,
    *,
    top_n: int = 5,
):
    """
    複数ランキングをまとめて表示（scheduler / debug 用）

    Args:
        ranking_map (dict):
            {
              "price_up": DataFrame,
              "volume": DataFrame,
              ...
            }
        top_n (int):
            各ランキングの表示件数
    """

    if not isinstance(ranking_map, dict):
        return

    for key, df in ranking_map.items():
        if df is None or df.empty:
            continue

        print_ranking_diff(
            df,
            top_n=top_n,
            title=f"RANKING [{key}]",
        )
