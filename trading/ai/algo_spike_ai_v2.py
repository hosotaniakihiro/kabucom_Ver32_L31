# ============================================================
# File   : trading/ai/algo_spike_ai_v2.py
# Version: Ver2.0-PRODUCTION-ALGO-SPIKE-DETECTOR
# ------------------------------------------------------------
# アルゴスパイク検出AI
#
# 検出ロジック
#  ・tick急増
#  ・出来高急増
#  ・orderflow imbalance
#  ・spread compression
#
# 出力
#  algo_spike_score
#  algo_spike (0/1)
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

class AlgoSpikeAIV2:

    def __init__(self):

        # detection parameters
        self.tick_spike = 5
        self.volume_spike = 3
        self.orderflow_ratio = 0.75
        self.spread_threshold = 0.002


    # ========================================================
    # detect
    # ========================================================

    def detect(self, df: pd.DataFrame) -> pd.DataFrame:

        if df is None or df.empty:
            return df

        df = df.copy()

        try:

            df["algo_spike_score"] = 0.0

            # ------------------------------------------------
            # 1 tick spike
            # ------------------------------------------------

            if "tick_count" in df.columns:

                tick = _safe_numeric(df["tick_count"])

                df.loc[
                    tick > self.tick_spike,
                    "algo_spike_score"
                ] += 1


            # ------------------------------------------------
            # 2 volume spike
            # ------------------------------------------------

            if "volume_ratio" in df.columns:

                vol = _safe_numeric(df["volume_ratio"])

                df.loc[
                    vol > self.volume_spike,
                    "algo_spike_score"
                ] += 1


            # ------------------------------------------------
            # 3 orderflow imbalance
            # ------------------------------------------------

            if {"buy_volume", "sell_volume"} <= set(df.columns):

                buy = _safe_numeric(df["buy_volume"]).fillna(0)
                sell = _safe_numeric(df["sell_volume"]).fillna(0)

                ratio = buy / (buy + sell + 1)

                df.loc[
                    ratio > self.orderflow_ratio,
                    "algo_spike_score"
                ] += 1


            # ------------------------------------------------
            # 4 spread compression
            # ------------------------------------------------

            if {"bid", "ask"} <= set(df.columns):

                bid = _safe_numeric(df["bid"])
                ask = _safe_numeric(df["ask"])

                spread = (ask - bid) / (bid + 1e-9)

                df.loc[
                    spread < self.spread_threshold,
                    "algo_spike_score"
                ] += 1


            # ------------------------------------------------
            # final signal
            # ------------------------------------------------

            df["algo_spike"] = (
                df["algo_spike_score"] >= 2
            ).astype(int)


        except Exception:

            logger.exception(
                "[ALGO SPIKE] detection failed"
            )

        return df


# ============================================================
# singleton
# ============================================================

algo_spike_ai_v2 = AlgoSpikeAIV2()