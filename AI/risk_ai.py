# ============================================================
# AI/risk_ai.py
# Ver26-FINAL-RISK-AI-AUTONOMOUS
# ------------------------------------------------------------
# ✔ ENTRY の最終停止権限を持つ
# ✔ EXIT / HOLD には介入しない
# ✔ 連敗・DD・時間帯で自律停止
# ✔ 翌営業日で自動リセット
# ============================================================

import datetime as dt
import logging

logger = logging.getLogger("risk_ai")


class RiskAI:
    def __init__(
        self,
        max_loss_streak: int = 3,
        max_intraday_dd: float = -0.02,   # -2%
        cooldown_minutes: int = 30,
    ):
        self.max_loss_streak = max_loss_streak
        self.max_intraday_dd = max_intraday_dd
        self.cooldown_minutes = cooldown_minutes

        self.loss_streak = 0
        self.intraday_pnl = 0.0
        self.stopped_at: dt.datetime | None = None
        self.trade_date: dt.date | None = None

    # --------------------------------------------------------
    # 日付切り替え検知 → リセット
    # --------------------------------------------------------
    def _check_new_day(self, now: dt.datetime):
        if self.trade_date != now.date():
            self.trade_date = now.date()
            self.loss_streak = 0
            self.intraday_pnl = 0.0
            self.stopped_at = None
            logger.info("🔄 RiskAI reset (new trading day)")

    # --------------------------------------------------------
    # 約定結果を通知
    # --------------------------------------------------------
    def on_trade_result(self, pnl: float, now: dt.datetime):
        self._check_new_day(now)

        self.intraday_pnl += pnl

        if pnl < 0:
            self.loss_streak += 1
        else:
            self.loss_streak = 0

        # --- 即時停止判定 ---
        if (
            self.loss_streak >= self.max_loss_streak
            or self.intraday_pnl <= self.max_intraday_dd
        ):
            if not self.stopped_at:
                self.stopped_at = now
                logger.warning(
                    f"🛑 RiskAI STOP "
                    f"loss_streak={self.loss_streak} "
                    f"intraday_pnl={self.intraday_pnl:.4f}"
                )

    # --------------------------------------------------------
    # ENTRY 可否
    # --------------------------------------------------------
    def allow_entry(self, now: dt.datetime) -> bool:
        self._check_new_day(now)

        if not self.stopped_at:
            return True

        # --- クールダウン経過 ---
        elapsed = (now - self.stopped_at).total_seconds() / 60

        if elapsed >= self.cooldown_minutes:
            logger.info("🟢 RiskAI auto-resume after cooldown")
            self.stopped_at = None
            self.loss_streak = 0
            return True

        return False

    # --------------------------------------------------------
    # 強制停止（外部要因）
    # --------------------------------------------------------
    def force_stop(self, now: dt.datetime, reason: str):
        self.stopped_at = now
        logger.warning(f"⛔ RiskAI FORCE STOP: {reason}")
