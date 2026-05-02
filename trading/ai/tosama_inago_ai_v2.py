# ============================================================
# File   : trading/ai/tosama_inago_ai_v2.py
# Version: Ver2.0-PRODUCTION-TOSAMA-INAGO-DETECTOR
# ------------------------------------------------------------
# 殿様イナゴ検出AI（高精度版）
#
# 検出ロジック
#  ・急騰
#  ・出来高スパイク
#  ・VWAP乖離
#  ・ATRスケール傾き
#  ・短期モメンタム
#
# 出力
#  inago_score
#  tosama_inago (0/1)
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

class TosamaInagoAIV2:

    def __init__(self):

        # パラメータ
        self.price_jump = 0.012
        self.volume_spike = 2.0
        self.vwap_dev = 0.01
        self.slope_threshold = 0.4

    # ========================================================
    # detect
    # ========================================================

    def detect(self, df: pd.DataFrame) -> pd.DataFrame:

        if df is None or df.empty:
            return df

        df = df.copy()

        try:

            df["inago_score"] = 0.0

            # ------------------------------------------------
            # 1. price jump
            # ------------------------------------------------

            if "return_1m" in df.columns:

                r = _safe_numeric(df["return_1m"])

                df.loc[
                    r > self.price_jump,
                    "inago_score"
                ] += 1


            # ------------------------------------------------
            # 2. volume spike
            # ------------------------------------------------

            if "volume_ratio" in df.columns:

                v = _safe_numeric(df["volume_ratio"])

                df.loc[
                    v > self.volume_spike,
                    "inago_score"
                ] += 1


            # ------------------------------------------------
            # 3. VWAP deviation
            # ------------------------------------------------

            if "vwap_deviation" in df.columns:

                dev = _safe_numeric(df["vwap_deviation"])

                df.loc[
                    dev.abs() > self.vwap_dev,
                    "inago_score"
                ] += 1


            # ------------------------------------------------
            # 4. slope momentum
            # ------------------------------------------------

            if "slope_atr_scaled" in df.columns:

                slope = _safe_numeric(df["slope_atr_scaled"])

                df.loc[
                    slope > self.slope_threshold,
                    "inago_score"
                ] += 1


            # ------------------------------------------------
            # signal
            # ------------------------------------------------

            df["tosama_inago"] = (
                df["inago_score"] >= 3
            ).astype(int)


        except Exception:

            logger.exception(
                "[TOSAMA INAGO] detection failed"
            )

        return df


# ============================================================
# singleton
# ============================================================

tosama_inago_ai_v2 = TosamaInagoAIV2()