# ============================================================
# File   : trading/risk/risk_guard.py
# Version: FINAL-ROBUST-RISK-GUARD
# ------------------------------------------------------------
# ✔ 最大DD制御
# ✔ 日次損失制限
# ✔ 例外耐性
# ============================================================

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def risk_ok(
    current_drawdown: float,
    max_drawdown: float = 0.2,
    daily_loss: float | None = None,
    max_daily_loss: float = 0.05,
) -> bool:

    try:
        if current_drawdown >= max_drawdown:
            return False

        if daily_loss is not None and daily_loss <= -abs(max_daily_loss):
            return False

        return True

    except Exception:
        logger.exception("[RISK_GUARD] failed")
        return False