# ============================================================
# File   : trading/risk/position_sizer_v65.py
# Version: V65-ADAPTIVE-POSITION-SIZER
# ------------------------------------------------------------
# ✔ ATRベース
# ✔ capital割合
# ✔ collapse縮小
# ✔ regime倍率
# ✔ throttle連動
# ✔ 連敗縮小
# ✔ ボラ過熱縮小
# ✔ NaN耐性
# ✔ thread safe
# ============================================================

from __future__ import annotations
import math
import logging
from threading import Lock

from core.global_context.context import global_context as GC
from trading.risk.risk_throttle_v2 import get_adaptive_risk_throttle

logger = logging.getLogger(__name__)


# ============================================================
# 安全数値
# ============================================================

def _safe(v, default=0.0):
    try:
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# AdaptivePositionSizer
# ============================================================

class AdaptivePositionSizer:

    def __init__(
        self,
        atr_multiplier: float = 2.0,
        min_size: int = 1,
        max_risk_per_trade: float = 0.01,  # 1%
    ):
        self.atr_multiplier = atr_multiplier
        self.min_size = min_size
        self.max_risk_per_trade = max_risk_per_trade
        self._lock = Lock()

    # ========================================================
    # regime倍率
    # ========================================================

    def _regime_multiplier(self, regime: int) -> float:

        regime = int(_safe(regime))

        if regime == 1:     # TREND
            return 1.2
        elif regime == 2:   # RANGE
            return 0.9
        elif regime == 3:   # CRASH
            return 0.5
        return 1.0

    # ========================================================
    # collapse縮小
    # ========================================================

    def _collapse_multiplier(self, collapse_strength: float) -> float:

        collapse_strength = _safe(collapse_strength)

        if collapse_strength <= 0:
            return 1.0

        # 線形縮小
        return max(0.3, 1.0 - collapse_strength)

    # ========================================================
    # 連敗縮小
    # ========================================================

    def _loss_multiplier(self) -> float:

        try:
            losses = _safe(GC.performance.get_consecutive_losses(), 0)

            if losses <= 0:
                return 1.0

            # 連敗ごとに10%縮小
            return max(0.5, 1.0 - losses * 0.1)

        except Exception:
            return 1.0

    # ========================================================
    # ボラ縮小
    # ========================================================

    def _volatility_multiplier(self, atr: float, price: float) -> float:

        atr = _safe(atr)
        price = _safe(price, 1e-6)

        if price <= 0:
            return 1.0

        ratio = atr / price

        if ratio > 0.05:
            return 0.7
        if ratio > 0.03:
            return 0.85

        return 1.0

    # ========================================================
    # メイン計算
    # ========================================================

    def calculate_size(
        self,
        *,
        price: float,
        atr: float,
        regime: int,
        collapse_strength: float = 0.0,
    ) -> int:

        with self._lock:

            price = _safe(price)
            atr = _safe(atr)

            if price <= 0 or atr <= 0:
                return 0

            throttle = get_adaptive_risk_throttle()

            if not throttle.allow_entry(
                atr=atr,
                price=price,
                regime=regime,
                collapse_strength=collapse_strength,
            ):
                return 0

            capital = _safe(GC.account.get_capital(), 0)

            risk_amount = capital * self.max_risk_per_trade
            stop_distance = atr * self.atr_multiplier

            if stop_distance <= 0:
                return 0

            base_size = risk_amount / stop_distance

            # 各種倍率
            mult = (
                self._regime_multiplier(regime)
                * self._collapse_multiplier(collapse_strength)
                * self._loss_multiplier()
                * self._volatility_multiplier(atr, price)
            )

            size = base_size * mult

            if size <= 0:
                return 0

            return max(int(size), self.min_size)


# ============================================================
# グローバル取得
# ============================================================

def get_adaptive_position_sizer() -> AdaptivePositionSizer:

    sizer = getattr(GC.risk, "adaptive_position_sizer", None)

    if sizer is None:
        sizer = AdaptivePositionSizer()
        GC.risk.adaptive_position_sizer = sizer

    return sizer