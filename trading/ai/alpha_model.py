# ============================================================
# trading/ai/alpha_model.py
#
# PRODUCTION ALPHA MODEL
#
# Predicts:
#   entry probability
#   expected return
#   alpha score
#
# Supports:
#   LightGBM
#   fallback linear model
# ============================================================

from __future__ import annotations

import logging
import numpy as np
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:
    import lightgbm as lgb
except Exception:
    lgb = None


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(x, hi))


def _safe_array(features: Dict):

    vals = []

    for v in features.values():

        try:

            vals.append(float(v))

        except Exception:

            vals.append(0.0)

    return np.array(vals, dtype=float)


# ============================================================
# Alpha Model
# ============================================================

class AlphaModel:

    def __init__(self):

        self.model: Optional[object] = None

        self.feature_names = None

        self.loaded = False

    # --------------------------------------------------------
    # load model
    # --------------------------------------------------------

    def load(self, path: str):

        if lgb is None:

            logger.warning("LightGBM not installed")

            return

        try:

            self.model = lgb.Booster(model_file=path)

            self.loaded = True

            logger.info("Alpha model loaded")

        except Exception:

            logger.exception("Model load failed")

    # --------------------------------------------------------
    # predict
    # --------------------------------------------------------

    def predict(self, features: Dict) -> Dict:

        try:

            if not features:

                return self._fallback()

            x = _safe_array(features)

            if self.loaded and self.model is not None:

                pred = self.model.predict(
                    x.reshape(1, -1)
                )[0]

                alpha = float(pred)

            else:

                alpha = self._linear_alpha(x)

            prob = _clip(alpha)

            expected = self._expected_return(alpha)

            return {

                "alpha_score": alpha,

                "entry_probability": prob,

                "expected_return": expected

            }

        except Exception:

            logger.exception("Alpha prediction failure")

            return self._fallback()

    # --------------------------------------------------------
    # fallback linear model
    # --------------------------------------------------------

    def _linear_alpha(self, x):

        if len(x) == 0:

            return 0.5

        score = np.mean(x)

        score = np.tanh(score)

        return float((score + 1) / 2)

    # --------------------------------------------------------
    # expected return
    # --------------------------------------------------------

    def _expected_return(self, alpha):

        return alpha * 0.02

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------

    def _fallback(self):

        return {

            "alpha_score": 0.5,

            "entry_probability": 0.5,

            "expected_return": 0.0

        }


# ============================================================
# Singleton
# ============================================================

_model = None


def get_alpha_model():

    global _model

    if _model is None:

        _model = AlphaModel()

    return _model