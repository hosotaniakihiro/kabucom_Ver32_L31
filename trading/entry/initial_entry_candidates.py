# ============================================================
# File   : trading/entry/initial_entry_candidates.py
# Version: Ver2.0-PRODUCTION-INITIAL-ENTRY-CANDIDATES-FULL
# ------------------------------------------------------------
# ✔ 既存ロジック完全保持（削除ゼロ）
# ✔ score_buy ベース選別維持
# ✔ active symbol 制限維持
# ✔ acceleration フィルタ追加（最重要）
# ✔ trend / momentum フィルタ追加
# ✔ volume スパイク対応
# ✔ NaN / inf 完全防御
# ✔ ソート安定化
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    try:
        if col in df.columns:
            return (
                pd.to_numeric(df[col], errors="coerce")
                .replace([np.inf, -np.inf], 0)
                .fillna(0)
            )
    except Exception:
        pass

    return pd.Series(0.0, index=df.index)


# ============================================================
# main
# ============================================================

def get_initial_entry_candidates(
    interval: int = 5,
    score_th: float = 5.0,
    top_n: int = 10,
) -> pd.DataFrame:

    try:

        df = global_data.get_merged_summary(interval)

        if df is None or df.empty:
            return pd.DataFrame()

        df = df.copy()

        # ----------------------------------------------------
        # safe columns
        # ----------------------------------------------------

        score_buy = _safe_series(df, "score_buy")
        symbol = df["symbol"] if "symbol" in df.columns else pd.Series([], dtype=str)

        # ----------------------------------------------------
        # 既存条件（完全保持）
        # ----------------------------------------------------

        cond = (
            (score_buy >= score_th)
            & (symbol.isin(global_data.symbols_active))
        )

        df = df[cond]

        if df.empty:
            return df

        # ----------------------------------------------------
        # 追加フィルタ（強化）
        # ----------------------------------------------------

        trend = _safe_series(df, "_score_trend")
        momentum = _safe_series(df, "_score_momentum")
        acceleration = _safe_series(df, "_score_acceleration")

        # 🔥 コアフィルタ（超重要）
        df = df[
            (trend > 0.2)
            & (momentum > 0.2)
            & (acceleration > 0)
        ]

        if df.empty:
            return df

        # ----------------------------------------------------
        # volume スパイク
        # ----------------------------------------------------

        if "volume" in df.columns:
            volume = _safe_series(df, "volume")
            vol_ma = volume.rolling(5).mean()

            vol_ratio = volume / (vol_ma + 1e-9)

            df = df[vol_ratio > 1.1]

            if df.empty:
                return df

        # ----------------------------------------------------
        # ソート（安定化）
        # ----------------------------------------------------

        if "score" in df.columns:
            sort_key = "score"
        else:
            sort_key = "score_buy"

        df = df.sort_values(
            by=[sort_key, "score_buy"],
            ascending=False,
            kind="mergesort",  # 安定ソート
        )

        # ----------------------------------------------------
        # 最終
        # ----------------------------------------------------

        df = df.head(top_n).reset_index(drop=True)

        logger.info(
            "[ENTRY INIT] interval=%s candidates=%s",
            interval,
            len(df),
        )

        return df

    except Exception:
        logger.exception("[initial_entry_candidates] failed")
        return pd.DataFrame()