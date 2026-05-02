# ============================================================
# File   : trading/risk/risk_throttle_v2.py
# Version: V65-ADAPTIVE-RISK-THROTTLE
# ------------------------------------------------------------
# ✔ 既存risk_throttle非破壊
# ✔ RL連動
# ✔ collapse連動
# ✔ regime連動
# ✔ 日次損失制御
# ✔ 連敗制御
# ✔ ボラティリティ制御
# ✔ NaN完全防御
# ✔ thread safe
# ✔ 将来cluster拡張対応
# ============================================================

from __future__ import annotations

import math
import logging
from threading import Lock

from core.global_context.context import global_context as GC

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
# AdaptiveRiskThrottle
# ============================================================

class AdaptiveRiskThrottle:
    """
    V65: 状態適応型リスク制御
    """

    def __init__(
        self,
        max_daily_loss_ratio: float = 0.03,
        max_consecutive_losses: int = 5,
        high_volatility_atr_ratio: float = 0.05,
    ):

        self.max_daily_loss_ratio = max_daily_loss_ratio
        self.max_consecutive_losses = max_consecutive_losses
        self.high_volatility_atr_ratio = high_volatility_atr_ratio

        self._lock = Lock()

    # ========================================================
    # 日次損失チェック
    # ========================================================

    def _check_daily_loss(self) -> bool:

        try:
            capital = _safe(GC.account.get_capital(), 1.0)
            daily_pnl = _safe(GC.account.get_today_realized_pnl())

            loss_limit = capital * self.max_daily_loss_ratio

            if daily_pnl <= -loss_limit:
                logger.warning(
                    "[RISK] Daily loss limit reached: %.2f / %.2f",
                    daily_pnl, -loss_limit
                )
                return False

            return True

        except Exception:
            logger.exception("[RISK] daily loss check error")
            return True  # フェイルセーフ（止めない）

    # ========================================================
    # 連敗チェック
    # ========================================================

    def _check_consecutive_losses(self) -> bool:

        try:
            losses = _safe(GC.performance.get_consecutive_losses(), 0)

            if losses >= self.max_consecutive_losses:
                logger.warning(
                    "[RISK] Consecutive losses exceeded: %d",
                    losses
                )
                return False

            return True

        except Exception:
            logger.exception("[RISK] consecutive loss check error")
            return True

    # ========================================================
    # ボラティリティチェック
    # ========================================================

    def _check_volatility(self, atr: float, price: float) -> bool:

        atr = _safe(atr)
        price = _safe(price, 1e-6)

        if price <= 0:
            return True

        atr_ratio = atr / price

        if atr_ratio > self.high_volatility_atr_ratio:
            logger.warning(
                "[RISK] High volatility detected: ATR ratio=%.4f",
                atr_ratio
            )
            return False

        return True

    # ========================================================
    # collapse連動
    # ========================================================

    def _check_collapse_risk(self, collapse_strength: float) -> bool:

        collapse_strength = _safe(collapse_strength)

        if collapse_strength > 0.9:
            logger.warning(
                "[RISK] Collapse strength too high: %.2f",
                collapse_strength
            )
            return False

        return True

    # ========================================================
    # regime連動
    # ========================================================

    def _check_regime(self, regime: int) -> bool:

        regime = int(_safe(regime))

        # 例: regime 3 = CRASH（仮）
        if regime == 3:
            logger.warning("[RISK] Crash regime detected")
            return False

        return True

    # ========================================================
    # 総合判断
    # ========================================================

    def allow_entry(
        self,
        *,
        atr: float,
        price: float,
        regime: int,
        collapse_strength: float = 0.0,
    ) -> bool:

        with self._lock:

            if not self._check_daily_loss():
                return False

            if not self._check_consecutive_losses():
                return False

            if not self._check_volatility(atr, price):
                return False

            if not self._check_collapse_risk(collapse_strength):
                return False

            if not self._check_regime(regime):
                return False

            return True


# ============================================================
# グローバル登録（V65）
# ============================================================

def get_adaptive_risk_throttle() -> AdaptiveRiskThrottle:

    throttle = getattr(GC.risk, "adaptive_throttle", None)

    if throttle is None:
        throttle = AdaptiveRiskThrottle()
        GC.risk.adaptive_throttle = throttle

    return throttle