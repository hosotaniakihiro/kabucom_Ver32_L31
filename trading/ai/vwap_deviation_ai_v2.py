# ============================================================
# File   : trading/ai/vwap_deviation_ai_v2.py
# Version: Ver2.0-PRODUCTION-VWAP-DEVIATION-AI
# ------------------------------------------------------------
# VWAP乖離AI
#
# 検出
#  ・VWAP乖離
#  ・極端乖離
#  ・VWAPブレイク
#  ・リバート候補
#
# 出力
#  vwap_deviation
#  vwap_dev_score
#  vwap_signal
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
# VWAP Deviation Detector
# ============================================================

class VWAPDeviationAIV2:

    def __init__(self):

        # VWAP deviation thresholds
        self.dev_threshold = 0.01
        self.extreme_dev = 0.025
        self.reversion_threshold = 0.005


    # ========================================================
    # detect
    # ========================================================

    def detect(self, df: pd.DataFrame) -> pd.DataFrame:

        if df is None or df.empty:
            return df

        df = df.copy()

        try:

            df["vwap_dev_score"] = 0.0

            # ------------------------------------------------
            # VWAP deviation calculation
            # ------------------------------------------------

            if {"close", "vwap"} <= set(df.columns):

                close = _safe_numeric(df["close"])
                vwap = _safe_numeric(df["vwap"])

                deviation = (close - vwap) / (vwap + 1e-9)

                df["vwap_deviation"] = deviation


                # basic deviation
                df.loc[
                    deviation.abs() > self.dev_threshold,
                    "vwap_dev_score"
                ] += 1


                # extreme deviation
                df.loc[
                    deviation.abs() > self.extreme_dev,
                    "vwap_dev_score"
                ] += 1


                # mean reversion candidate
                df.loc[
                    deviation.abs() < self.reversion_threshold,
                    "vwap_dev_score"
                ] += 0.5


            # ------------------------------------------------
            # VWAP breakout
            # ------------------------------------------------

            if {"close", "vwap", "prev_close"} <= set(df.columns):

                close = _safe_numeric(df["close"])
                vwap = _safe_numeric(df["vwap"])
                prev_close = _safe_numeric(df["prev_close"])

                cross_up = (prev_close < vwap) & (close > vwap)
                cross_down = (prev_close > vwap) & (close < vwap)

                df.loc[cross_up, "vwap_dev_score"] += 1
                df.loc[cross_down, "vwap_dev_score"] += 1


            # ------------------------------------------------
            # final signal
            # ------------------------------------------------

            df["vwap_signal"] = (
                df["vwap_dev_score"] >= 2
            ).astype(int)


        except Exception:

            logger.exception(
                "[VWAP DEVIATION AI] detection failed"
            )

        return df


# ============================================================
# singleton
# ============================================================

vwap_deviation_ai_v2 = VWAPDeviationAIV2()