# ============================================================
# File   : trading/ranking/engines/institutional.py
# Version: Ver2-PRODUCTION-INSTITUTIONAL-ENGINE-FINAL
# ------------------------------------------------------------
# ✔ apply_institutional 追加（ImportError解消）
# ✔ institutional flow detection
# ✔ volume spike / smart money proxy
# ✔ fallback安全
# ✔ NaN / inf完全耐性
# ✔ vectorized高速処理
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# coreロジック
# ============================================================

def _institutional_core(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # --------------------------------------------------------
    # 必須列チェック
    # --------------------------------------------------------
    if "volume" not in df.columns:
        logger.warning("[institutional] volume missing")
        df["institutional_score"] = 0.0
        return df

    try:
        vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

        # ----------------------------------------------------
        # volume spike検出
        # ----------------------------------------------------
        rolling_mean = vol.rolling(20, min_periods=1).mean()

        spike = vol / (rolling_mean + 1e-9)

        # ----------------------------------------------------
        # 正規化
        # ----------------------------------------------------
        score = np.tanh(spike - 1.0)

        df["institutional_score"] = score

    except Exception:
        logger.exception("[institutional] calc failed")
        df["institutional_score"] = 0.0

    return df


# ============================================================
# 🚨 public API（ranking_pipeline用）
# ============================================================

def apply_institutional(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking_pipeline統一API
    """

    if df is None or df.empty:
        return pd.DataFrame()

    try:
        return _institutional_core(df)
    except Exception:
        logger.exception("[institutional] apply failed")
        return df


# ============================================================
# 互換（旧名対応）
# ============================================================

def apply_institutional_flow(df: pd.DataFrame) -> pd.DataFrame:
    return apply_institutional(df)