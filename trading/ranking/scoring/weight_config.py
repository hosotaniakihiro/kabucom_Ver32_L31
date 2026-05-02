# ============================================================
# File   : trading/ranking/scoring/weight_config.py
# Version: Ver4.1-PRODUCTION-ALPHA-WEIGHT-CONFIG-FINAL-FIXED
# ------------------------------------------------------------
# ✔ Ver4 完全互換（削除ゼロ）
# ✔ acceleration重み追加
# ✔ velocity正式重み化
# ✔ ボラ調整強化
# ✔ safety強化
# ✔ AI拡張完全対応
# ✔ production hardened
# ✔ NEW: normalize safety floor
# ============================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)


# ============================================================
# default weights
# ============================================================

DEFAULT_WEIGHTS: Dict[str, float] = {
    "base": 0.35,
    "trend": 0.25,
    "momentum": 0.15,
    "velocity": 0.10,
    "acceleration": 0.10,
    "event": 0.03,
    "flow": 0.02,
}


# ============================================================
# regime weights
# ============================================================

REGIME_WEIGHTS = {

    "trend": {
        "base": 0.30,
        "trend": 0.30,
        "momentum": 0.20,
        "velocity": 0.10,
        "acceleration": 0.07,
        "event": 0.02,
        "flow": 0.01,
    },

    "range": {
        "base": 0.45,
        "trend": 0.10,
        "momentum": 0.10,
        "velocity": 0.10,
        "acceleration": 0.05,
        "event": 0.10,
        "flow": 0.10,
    },

    "crash": {
        "base": 0.45,
        "trend": 0.05,
        "momentum": 0.05,
        "velocity": 0.10,
        "acceleration": 0.05,
        "event": 0.10,
        "flow": 0.20,
    },
}


# ============================================================
# dataclass
# ============================================================

@dataclass
class WeightSet:
    base: float
    trend: float
    momentum: float
    event: float
    flow: float
    velocity: float = 0.0
    acceleration: float = 0.0

    def normalize(self) -> "WeightSet":
        values = [
            self.base,
            self.trend,
            self.momentum,
            self.event,
            self.flow,
            self.velocity,
            self.acceleration,
        ]

        total = sum(v for v in values if isinstance(v, (int, float)))

        if total <= 0:
            logger.warning("[weight_config] invalid total<=0, fallback to equal-ish safe defaults")
            return WeightSet(**DEFAULT_WEIGHTS).normalize() if values != list(DEFAULT_WEIGHTS.values()) else self

        return WeightSet(
            base=self.base / total,
            trend=self.trend / total,
            momentum=self.momentum / total,
            event=self.event / total,
            flow=self.flow / total,
            velocity=self.velocity / total,
            acceleration=self.acceleration / total,
        )


# ============================================================
# core
# ============================================================

def get_weight_set(
    regime: str | None = None
) -> WeightSet:
    """
    レジームに応じた重み取得
    """

    try:
        if regime and regime in REGIME_WEIGHTS:
            w = REGIME_WEIGHTS[regime]
        else:
            w = DEFAULT_WEIGHTS

        w_safe = {
            "base": float(w.get("base", 0)),
            "trend": float(w.get("trend", 0)),
            "momentum": float(w.get("momentum", 0)),
            "event": float(w.get("event", 0)),
            "flow": float(w.get("flow", 0)),
            "velocity": float(w.get("velocity", 0)),
            "acceleration": float(w.get("acceleration", 0)),
        }

        ws = WeightSet(**w_safe).normalize()

        return ws

    except Exception:
        logger.exception("[weight_config] fallback to default")
        return WeightSet(**DEFAULT_WEIGHTS).normalize()


# ============================================================
# volatility adjustment
# ============================================================

def adjust_weights_by_volatility(
    weights: WeightSet,
    volatility: float
) -> WeightSet:
    """
    ボラティリティに応じて調整
    """

    try:
        volatility = float(volatility)

        if volatility > 0.02:
            weights.momentum *= 1.2
            weights.acceleration *= 1.3
            weights.velocity *= 1.2

        elif volatility < 0.005:
            weights.base *= 1.2
            weights.trend *= 1.2

        return weights.normalize()

    except Exception:
        return weights


# ============================================================
# AI override
# ============================================================

def override_weights_from_model(
    weights: WeightSet,
    model_output: dict | None
) -> WeightSet:

    if not model_output:
        return weights

    try:
        for k, v in model_output.items():
            if hasattr(weights, k):
                setattr(weights, k, float(v))

        return weights.normalize()

    except Exception:
        logger.exception("[weight_config] AI override failed")
        return weights


# ============================================================
# debug
# ============================================================

def weight_to_dict(weights: WeightSet) -> Dict[str, float]:
    return {
        "base": weights.base,
        "trend": weights.trend,
        "momentum": weights.momentum,
        "velocity": weights.velocity,
        "acceleration": weights.acceleration,
        "event": weights.event,
        "flow": weights.flow,
    }
