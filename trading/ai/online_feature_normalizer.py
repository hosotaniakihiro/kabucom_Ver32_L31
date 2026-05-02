# ============================================================
# trading/ai/online_feature_normalizer.py
#
# PRODUCTION ONLINE FEATURE NORMALIZER
#
# Performs real-time normalization of feature streams
#
# Uses:
#   Welford online mean/variance
#   z-score normalization
#
# Used for:
#   AI inference
#   training consistency
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# Online Stats
# ============================================================

class OnlineStats:

    def __init__(self):

        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0

    # --------------------------------------------------------
    # update statistics
    # --------------------------------------------------------

    def update(self, value: float):

        self.count += 1

        delta = value - self.mean

        self.mean += delta / self.count

        delta2 = value - self.mean

        self.m2 += delta * delta2

    # --------------------------------------------------------
    # variance
    # --------------------------------------------------------

    def variance(self):

        if self.count < 2:

            return 0.0

        return self.m2 / (self.count - 1)

    # --------------------------------------------------------
    # std
    # --------------------------------------------------------

    def std(self):

        return math.sqrt(self.variance())


# ============================================================
# Online Feature Normalizer
# ============================================================

class OnlineFeatureNormalizer:

    def __init__(self):

        # feature -> stats
        self.stats: Dict[str, OnlineStats] = {}

        self.clip_range = 5.0

    # --------------------------------------------------------
    # normalize features
    # --------------------------------------------------------

    def normalize(self, features: Dict) -> Dict:

        normalized = {}

        for name, value in features.items():

            if value is None:

                normalized[name] = 0

                continue

            stats = self.stats.get(name)

            if stats is None:

                stats = OnlineStats()

                self.stats[name] = stats

            stats.update(float(value))

            std = stats.std()

            if std == 0:

                normalized[name] = 0

                continue

            z = (value - stats.mean) / std

            z = max(-self.clip_range, min(self.clip_range, z))

            normalized[name] = z

        return normalized

    # --------------------------------------------------------
    # reset stats
    # --------------------------------------------------------

    def reset(self):

        self.stats = {}

    # --------------------------------------------------------
    # get stats
    # --------------------------------------------------------

    def get_stats(self):

        result = {}

        for name, s in self.stats.items():

            result[name] = {

                "count": s.count,

                "mean": s.mean,

                "std": s.std()

            }

        return result


# ============================================================
# Singleton
# ============================================================

_normalizer = None


def get_online_feature_normalizer():

    global _normalizer

    if _normalizer is None:

        _normalizer = OnlineFeatureNormalizer()

    return _normalizer