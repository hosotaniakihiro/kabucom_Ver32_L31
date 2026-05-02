# ============================================================
# File   : trading/ai/absorption_ai.py
# Version: Ver1.0-PRODUCTION-INSTITUTIONAL-ABSORPTION
# ------------------------------------------------------------
# 板吸収AI（機関トレード検出）
#
# 検出ロジック
#  ・buy/sell imbalance
#  ・price stagnation
#  ・volume surge
#  ・spread stability
#
# 出力
#  absorption_score
#  absorption_signal
#
# 設計
#  ✔ vectorized
#  ✔ NaN/inf完全防御
#  ✔ 列欠損安全
#  ✔ logger互換
#  ✔ summary_controller互換
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe_numeric(series):

    if series is None:
        return None

    return pd.to_numeric(
        series,
        errors="coerce"
    ).replace(
        [np.inf, -np.inf],
        np.nan
    )


# ============================================================
# Detector
# ============================================================

class AbsorptionAI:

    def __init__(self):

        # 機関吸収パラメータ
        self.orderflow_ratio = 0.70
        self.volume_spike = 2.0
        self.price_stall = 0.002
        self.spread_threshold = 0.003


    # ========================================================
    # detect
    # ========================================================

    def detect(self, df: pd.DataFrame) -> pd.DataFrame:

        if df is None or df.empty:
            return df

        df = df.copy()

        try:

            df["absorption_score"] = 0.0

            # ------------------------------------------------
            # 1 orderflow imbalance
            # ------------------------------------------------

            if {"buy_volume", "sell_volume"} <= set(df.columns):

                buy = _safe_numeric(df["buy_volume"]).fillna(0)
                sell = _safe_numeric(df["sell_volume"]).fillna(0)

                ratio = buy / (buy + sell + 1)

                df.loc[
                    ratio > self.orderflow_ratio,
                    "absorption_score"
                ] += 1


            # ------------------------------------------------
            # 2 volume spike
            # ------------------------------------------------

            if "volume_ratio" in df.columns:

                vol = _safe_numeric(df["volume_ratio"])

                df.loc[
                    vol > self.volume_spike,
                    "absorption_score"
                ] += 1


            # ------------------------------------------------
            # 3 price stall
            # ------------------------------------------------

            if {"high", "low"} <= set(df.columns):

                high = _safe_numeric(df["high"])
                low = _safe_numeric(df["low"])

                stall = (high - low) / (low + 1e-9)

                df.loc[
                    stall < self.price_stall,
                    "absorption_score"
                ] += 1


            # ------------------------------------------------
            # 4 spread stability
            # ------------------------------------------------

            if {"bid", "ask"} <= set(df.columns):

                bid = _safe_numeric(df["bid"])
                ask = _safe_numeric(df["ask"])

                spread = (ask - bid) / (bid + 1e-9)

                df.loc[
                    spread < self.spread_threshold,
                    "absorption_score"
                ] += 1


            # ------------------------------------------------
            # signal
            # ------------------------------------------------

            df["absorption_signal"] = (
                df["absorption_score"] >= 2
            ).astype(int)


        except Exception:

            logger.exception(
                "[ABSORPTION AI] detection failed"
            )

        return df


# ============================================================
# singleton
# ============================================================

absorption_ai = AbsorptionAI()