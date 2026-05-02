# ============================================================
# File   : trading/risk/risk_engine.py
# Version: Ver1.3-PRODUCTION-RISK-ENGINE-HARDENED
# ------------------------------------------------------------
# ✔ ドローダウン制御
# ✔ 連敗制御
# ✔ Regime別リスク係数
# ✔ collapse連動縮小
# ✔ Boost連動制御
# ✔ サイズ倍率出力
# ✔ エントリー許可判定
# ✔ OMS互換 allow_order
# ✔ get_risk_engine singleton
# ✔ NaN/inf完全防御
# ✔ 本番例外耐性
# ============================================================

from __future__ import annotations

import logging
import math
import datetime as dt
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================
# SAFE UTIL
# ============================================================

def _safe(v, default=0.0):

    try:

        if v is None:
            return default

        v = float(v)

        if math.isnan(v) or math.isinf(v):
            return default

        return v

    except Exception:

        return default


def _clamp(v, lo, hi):

    return max(lo, min(hi, v))


# ============================================================
# RESULT OBJECT
# ============================================================

@dataclass
class RiskResult:

    allow_entry: bool
    size_multiplier: float
    reason: str


# ============================================================
# RISK ENGINE
# ============================================================

class RiskEngine:

    # --------------------------------------------------------
    # 設定値
    # --------------------------------------------------------

    MAX_DRAWDOWN_STOP = -0.15
    REDUCE_DRAWDOWN = -0.08

    MAX_CONSECUTIVE_LOSS = 5
    LOSS_REDUCE_START = 2

    COLLAPSE_SHRINK_LEVEL = 0.6

    REGIME_RISK_MAP = {

        0: 1.0,   # normal
        1: 1.1,   # bullish
        2: 0.8,   # range
        3: 0.6,   # unstable
        4: 0.5,   # crash

    }

    # --------------------------------------------------------

    def __init__(self):

        self.lock = threading.RLock()
        self.last_update = None

    # ========================================================
    # メイン評価
    # ========================================================

    def evaluate(
        self,
        *,
        drawdown: float,
        consecutive_losses: int,
        regime: int,
        collapse_prob: float,
        boost_active: bool = False,
    ) -> dict:

        try:

            drawdown = _safe(drawdown)
            collapse_prob = _clamp(_safe(collapse_prob), 0.0, 1.0)

            consecutive_losses = int(consecutive_losses or 0)
            regime = int(regime or 0)

            result = {

                "allow_entry": True,
                "size_multiplier": 1.0,
                "reason": "OK",

            }

            # =================================================
            # 完全停止条件
            # =================================================

            if drawdown <= self.MAX_DRAWDOWN_STOP:

                result["allow_entry"] = False
                result["size_multiplier"] = 0.0
                result["reason"] = "MAX_DRAWDOWN_STOP"

                logger.warning(
                    "[RISK] Trading halted: drawdown %.3f",
                    drawdown,
                )

                return result

            if consecutive_losses >= self.MAX_CONSECUTIVE_LOSS:

                result["allow_entry"] = False
                result["size_multiplier"] = 0.0
                result["reason"] = "MAX_CONSECUTIVE_LOSS"

                logger.warning(
                    "[RISK] Trading halted: consecutive losses %s",
                    consecutive_losses,
                )

                return result

            # =================================================
            # サイズ縮小ロジック
            # =================================================

            size = 1.0

            # ---------------------------------
            # Drawdown reduction
            # ---------------------------------

            if drawdown <= self.REDUCE_DRAWDOWN:

                dd_ratio = abs(drawdown / self.MAX_DRAWDOWN_STOP)

                size *= max(0.4, 1.0 - dd_ratio)

            # ---------------------------------
            # Loss streak reduction
            # ---------------------------------

            if consecutive_losses >= self.LOSS_REDUCE_START:

                loss_ratio = min(0.5, consecutive_losses * 0.1)

                size *= max(0.5, 1.0 - loss_ratio)

            # ---------------------------------
            # Regime adjustment
            # ---------------------------------

            regime_factor = self.REGIME_RISK_MAP.get(regime, 0.8)

            size *= regime_factor

            # ---------------------------------
            # Collapse protection
            # ---------------------------------

            if collapse_prob > self.COLLAPSE_SHRINK_LEVEL:

                shrink = min(0.5, collapse_prob)

                size *= max(0.3, 1.0 - shrink)

            # ---------------------------------
            # Boost modifier
            # ---------------------------------

            if boost_active:

                size *= 1.3

            # ---------------------------------

            size = _clamp(size, 0.1, 2.0)

            result["size_multiplier"] = size

            # =================================================
            # Entry許可判定
            # =================================================

            if size <= 0.2:

                result["allow_entry"] = False
                result["reason"] = "RISK_TOO_HIGH"

            return result

        except Exception:

            logger.exception("[RISK] evaluate failure")

            return {

                "allow_entry": False,
                "size_multiplier": 0.0,
                "reason": "RISK_ENGINE_ERROR",

            }

    # ========================================================
    # OMS互換
    # ========================================================

    def allow_order(self, symbol, side, quantity) -> bool:

        try:

            quantity = int(quantity)

            if quantity <= 0:
                return False

            if quantity > 10000:

                logger.warning(
                    "[RISK] excessive order size blocked %s",
                    quantity,
                )

                return False

            return True

        except Exception:

            logger.exception("[RISK] allow_order failure")

            return False


# ============================================================
# SINGLETON
# ============================================================

_risk_engine = None


def get_risk_engine():

    global _risk_engine

    if _risk_engine is None:

        _risk_engine = RiskEngine()

    return _risk_engine


# 互換用
risk_engine = get_risk_engine()