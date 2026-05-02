# ============================================================
# trading/ai/microstructure/toxicity_model.py
# PRODUCTION MARKET TOXICITY MODEL
#
# Computes market toxicity from microstructure signals
#
# Uses:
#   algo activity
#   spoof score
#   iceberg probability
#   orderflow imbalance
#   cancel ratio
#   board update speed
#   aggressive flow
#
# Output:
#   toxicity score (0 - 1)
# ============================================================

from __future__ import annotations

import numpy as np
import logging
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


# ============================================================
# Toxicity Model
# ============================================================

class ToxicityModel:

    def __init__(self):

        # weights
        self.w_algo = 0.30
        self.w_spoof = 0.20
        self.w_iceberg = 0.15
        self.w_orderflow = 0.15
        self.w_cancel = 0.10
        self.w_update = 0.10

        # thresholds
        self.high_toxicity = 0.75
        self.medium_toxicity = 0.45

    # --------------------------------------------------------
    # main compute
    # --------------------------------------------------------

    def compute(
        self,
        algo_score: float,
        spoof_score: float,
        iceberg_score: float,
        orderflow_score: float,
        cancel_ratio: float,
        board_update_score: float,
    ) -> Dict:

        try:

            algo = _clip(algo_score)
            spoof = _clip(spoof_score)
            iceberg = _clip(iceberg_score)
            flow = abs(orderflow_score)
            cancel = _clip(cancel_ratio)
            update = _clip(board_update_score)

            toxicity = (

                algo * self.w_algo
                + spoof * self.w_spoof
                + iceberg * self.w_iceberg
                + flow * self.w_orderflow
                + cancel * self.w_cancel
                + update * self.w_update

            )

            toxicity = _clip(toxicity)

            level = self._classify(toxicity)

            return {

                "toxicity": float(toxicity),

                "toxicity_level": level,

                "components": {

                    "algo": algo,
                    "spoof": spoof,
                    "iceberg": iceberg,
                    "orderflow": flow,
                    "cancel_ratio": cancel,
                    "update_speed": update,

                }

            }

        except Exception:

            logger.exception("ToxicityModel failure")

            return {
                "toxicity": 0.0,
                "toxicity_level": "LOW"
            }

    # --------------------------------------------------------
    # classification
    # --------------------------------------------------------

    def _classify(self, toxicity):

        if toxicity > self.high_toxicity:
            return "HIGH"

        if toxicity > self.medium_toxicity:
            return "MEDIUM"

        return "LOW"


# ============================================================
# Advanced Toxicity (optional)
# ============================================================

class AdvancedToxicityModel(ToxicityModel):

    def compute_advanced(
        self,
        algo_score,
        spoof_score,
        iceberg_score,
        orderflow_score,
        cancel_ratio,
        board_update_score,
        aggressive_ratio,
        flow_momentum
    ) -> Dict:

        base = super().compute(
            algo_score,
            spoof_score,
            iceberg_score,
            orderflow_score,
            cancel_ratio,
            board_update_score
        )

        try:

            agg = _clip(aggressive_ratio)

            momentum = abs(flow_momentum)

            enhancement = (

                agg * 0.15
                + momentum * 0.10

            )

            toxicity = _clip(base["toxicity"] + enhancement)

            level = self._classify(toxicity)

            base["toxicity"] = toxicity
            base["toxicity_level"] = level

            base["components"]["aggressive_ratio"] = agg
            base["components"]["flow_momentum"] = momentum

            return base

        except Exception:

            logger.exception("Advanced toxicity failure")

            return base


# ============================================================
# Singleton
# ============================================================

_model = None


def get_toxicity_model():

    global _model

    if _model is None:

        _model = ToxicityModel()

    return _model