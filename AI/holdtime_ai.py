# ============================================================
# AI/holdtime_ai.py
# (Ver26-FINAL-HOLDTIME-AI)
# ============================================================

from datetime import datetime, timedelta


class HoldTimeAI:
    def __init__(self):
        pass

    def decide(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        entry_time: datetime,
        now: datetime,
        atr: float | None = None,
    ) -> str:
        """
        return:
          HOLD
          EXIT_TAKE
          EXIT_LOSS
          EXIT_TIME
        """

        hold_sec = (now - entry_time).total_seconds()

        # -------------------------------
        # 共通：時間切れ
        # -------------------------------
        if hold_sec >= 900:  # 15分
            return "EXIT_TIME"

        pnl = (
            (current_price - entry_price)
            if side == "BUY"
            else (entry_price - current_price)
        )

        # -------------------------------
        # 利確（早い利益は逃がさない）
        # -------------------------------
        if pnl > (atr or entry_price * 0.002):
            return "EXIT_TAKE"

        # -------------------------------
        # 損切（加速損は即切り）
        # -------------------------------
        if pnl < -(atr or entry_price * 0.0015):
            return "EXIT_LOSS"

        return "HOLD"
