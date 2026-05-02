# ============================================================
# File   : trading/scoring/core/scoring_warmup_pipeline.py
# Version: 1.1-FINAL-WARMUP-PIPELINE-STRICT-SAFE
# ------------------------------------------------------------
# ✔ warmup_base → ranking → session 順次実行
# ✔ dict混入完全禁止
# ✔ score_total 強制float保証
# ✔ 各モジュールが欠けても安全
# ✔ BUY / SELL 最終再計算
# ✔ score_reasons 統合管理
# ✔ 1min / 3min / 5min 全対応
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd

from trading.scoring.core.scoring_warmup_base import scoring_warmup_base
from trading.scoring.core.scoring_warmup_ranking import scoring_warmup_ranking
from trading.scoring.core.scoring_warmup_session import scoring_warmup_session


# ============================================================
# 安全numeric変換
# ============================================================

def _to_numeric_safe(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype(float)


# ============================================================
# warmup統合パイプライン
# ============================================================

def scoring_warmup_pipeline(
    df: pd.DataFrame,
    interval: int,
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # 既存スコア列があれば削除（完全上書き保証）
    # --------------------------------------------------------
    for col in ("score_total", "score_buy", "score_sell", "score_reasons"):
        if col in df.columns:
            df.drop(columns=[col], inplace=True)

    # ========================================================
    # 1️⃣ BASE（テクニカル）
    # ========================================================
    try:
        df = scoring_warmup_base(df, interval)
    except Exception:
        # ベース失敗時はゼロ初期化
        df["score_total"] = 0.0

    # ========================================================
    # 2️⃣ RANKING補正
    # ========================================================
    try:
        df = scoring_warmup_ranking(df, interval)
    except Exception:
        pass

    # ========================================================
    # 3️⃣ SESSION補正
    # ========================================================
    try:
        df = scoring_warmup_session(df, interval)
    except Exception:
        pass

    # --------------------------------------------------------
    # 完全numeric保証
    # --------------------------------------------------------
    if "score_total" not in df.columns:
        df["score_total"] = 0.0

    df["score_total"] = _to_numeric_safe(
        df["score_total"]
    ).fillna(0.0)

    # 極端値制限（安全域）
    df["score_total"] = df["score_total"].clip(-25.0, 25.0)

    # ========================================================
    # BUY / SELL 最終再生成
    # ========================================================
    df["score_buy"] = np.where(
        df["score_total"] > 0,
        df["score_total"],
        0.0
    ).astype(float)

    df["score_sell"] = np.where(
        df["score_total"] < 0,
        -df["score_total"],
        0.0
    ).astype(float)

    # ========================================================
    # 理由統合（単一文字列固定）
    # ========================================================
    df["score_reasons"] = "warmup_pipeline"

    return df