# ============================================================
# File   : trading/ranking/execution/symbol_rotation.py
# Version: Ver1-PRODUCTION-SYMBOL-ROTATION
# ------------------------------------------------------------
# ✔ rankingベース銘柄ローテーション
# ✔ 上位N銘柄抽出
# ✔ 重複防止
# ✔ fallback安全
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# main API
# ============================================================

def rotate_symbols(
    df_ranking: pd.DataFrame,
    *,
    top_n: int = 50,
    score_col: str = "score",
    symbol_col: str = "symbol",
) -> list[str]:
    """
    rankingからアクティブ銘柄を選出
    """

    if df_ranking is None or df_ranking.empty:
        return []

    try:
        df = df_ranking.copy()

        # ----------------------------------------------------
        # 必須列チェック
        # ----------------------------------------------------
        if symbol_col not in df.columns:
            logger.warning("[symbol_rotation] symbol missing")
            return []

        if score_col not in df.columns:
            logger.warning("[symbol_rotation] score missing → fallback")
            df[score_col] = 0

        # ----------------------------------------------------
        # 型安全化
        # ----------------------------------------------------
        df[symbol_col] = df[symbol_col].astype(str)

        df[score_col] = pd.to_numeric(
            df[score_col], errors="coerce"
        ).fillna(0)

        # ----------------------------------------------------
        # ソート
        # ----------------------------------------------------
        df = df.sort_values(score_col, ascending=False)

        # ----------------------------------------------------
        # 上位抽出
        # ----------------------------------------------------
        df_top = df.head(top_n)

        # ----------------------------------------------------
        # 重複排除
        # ----------------------------------------------------
        symbols = (
            df_top[symbol_col]
            .drop_duplicates()
            .tolist()
        )

        return symbols

    except Exception:
        logger.exception("[symbol_rotation] failed")
        return []