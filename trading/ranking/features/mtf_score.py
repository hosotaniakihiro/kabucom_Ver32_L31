# ============================================================
# File   : trading/ranking/features/mtf_score.py
# Version: Ver3-PRODUCTION-ULTRA-STABLE-MTF-SCORE
# ------------------------------------------------------------
# ✔ MTFトレンド判定（MA25 / MA75）
# ✔ 上位足整合（3min / 5min 任意）
# ✔ slope方向一致
# ✔ fallback安全
# ✔ NaN / inf 完全防御
# ✔ dtype崩壊耐性
# ✔ pandas alignment crash防止
# ✔ スコア暴走防止（clip）
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

W_CROSS = 1.0          # MAクロス
W_SLOPE = 0.5          # 傾き
W_HIGHER_TF = 1.5      # 上位足整合

SCORE_CLIP_MIN = -10
SCORE_CLIP_MAX = 10


# ============================================================
# helpers
# ============================================================

def _safe_col(df: pd.DataFrame, col: str, default=0):
    if col in df.columns:
        return df[col]
    return pd.Series(default, index=df.index)


def _sanitize(s: pd.Series) -> pd.Series:
    return (
        s.replace([np.inf, -np.inf], np.nan)
         .fillna(0)
    )


# ============================================================
# main
# ============================================================

def build_mtf_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    MTFスコア生成

    - MA25 > MA75 → 上昇トレンド
    - slope正 → 上昇加速
    - 上位足整合 → ボーナス

    出力:
        df["score_mtf"]
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # MAクロス
        # ----------------------------------------------------
        ma25 = _safe_col(df, "ma25")
        ma75 = _safe_col(df, "ma75")

        cross = (ma25 > ma75).astype(int) - (ma25 < ma75).astype(int)
        cross = _sanitize(cross)

        # ----------------------------------------------------
        # slope（短期傾き）
        # ----------------------------------------------------
        slope = _safe_col(df, "ma25_slope")

        slope_score = np.sign(slope)
        slope_score = _sanitize(slope_score)

        # ----------------------------------------------------
        # 上位足整合（任意列）
        # ----------------------------------------------------
        # 例:
        # score_mtf_3min
        # score_mtf_5min
        higher_tf_score = pd.Series(0, index=df.index)

        for col in ["score_mtf_3min", "score_mtf_5min"]:

            if col in df.columns:
                higher_tf_score += _sanitize(df[col])

        # 正規化（列数依存防止）
        if higher_tf_score.abs().max() > 0:
            higher_tf_score = higher_tf_score / max(1, higher_tf_score.abs().max())

        # ----------------------------------------------------
        # 合成
        # ----------------------------------------------------
        score = (
            cross * W_CROSS
            + slope_score * W_SLOPE
            + higher_tf_score * W_HIGHER_TF
        )

        score = _sanitize(score)

        # ----------------------------------------------------
        # clip
        # ----------------------------------------------------
        score = score.clip(SCORE_CLIP_MIN, SCORE_CLIP_MAX)

        df["score_mtf"] = score

        return df

    except Exception:

        logger.exception("[mtf_score] failed")

        df["score_mtf"] = 0
        return df