# ============================================================
# File: AI/orchestrator_tonosama.py
# ------------------------------------------------------------
# 殿様イナゴ（BUY）専用 オーケストレーター
#
# ✔ ENTRY の最終許可を一元管理
# ✔ 連敗・日次損失・地合いで自動停止
# ✔ ENTRY / EXIT ロジックとは完全分離
# ✔ 人間の裁量介入ポイントを完全排除
# ============================================================

from __future__ import annotations

import datetime as dt


# ============================================================
# 固定パラメータ（絶対に変更しない）
# ============================================================

# 日次最大損失（-2%）
MAX_DAILY_LOSS = -0.02

# 連敗停止
MAX_CONSECUTIVE_LOSSES = 3

# 新規 ENTRY 停止時刻（後場終盤）
NO_ENTRY_AFTER = dt.time(14, 50)

# 地合い悪化判定
MIN_NIKKEI_VELOCITY = -0.002


# ============================================================
# オーケストレーター
# ============================================================

class TonosamaOrchestrator:
    """
    殿様イナゴ BUY の稼働可否を管理する司令塔
    """

    def __init__(self) -> None:
        self.enabled: bool = True
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.last_trade_date: dt.date | None = None

    # --------------------------------------------------------
    # 日付管理
    # --------------------------------------------------------

    def _ensure_new_day(self, now: dt.datetime) -> None:
        """
        日付が変わったら状態をリセット
        """
        today = now.date()
        if self.last_trade_date != today:
            self.enabled = True
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self.last_trade_date = today

    # --------------------------------------------------------
    # トレード結果通知
    # --------------------------------------------------------

    def on_trade_result(
        self,
        *,
        pnl: float,
        now: dt.datetime,
    ) -> None:
        """
        1トレード終了時に必ず呼ぶ

        Parameters
        ----------
        pnl : float
            1トレードの損益率（+/-）
        now : datetime
            現在時刻
        """

        self._ensure_new_day(now)

        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        # 連敗停止
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.enabled = False

        # 日次損失停止
        if self.daily_pnl <= MAX_DAILY_LOSS:
            self.enabled = False

    # --------------------------------------------------------
    # ENTRY 可否判定
    # --------------------------------------------------------

    def allow_entry(
        self,
        *,
        now: dt.datetime,
        nikkei_velocity: float | None = None,
    ) -> bool:
        """
        新規 ENTRY を許可するか判定

        Parameters
        ----------
        now : datetime
            現在時刻
        nikkei_velocity : float, optional
            日経平均の直近1分変化率

        Returns
        -------
        bool
            True  : ENTRY 許可
            False : ENTRY 禁止
        """

        self._ensure_new_day(now)

        if not self.enabled:
            return False

        # 時間帯ガード（後場終盤は入らない）
        if now.time() >= NO_ENTRY_AFTER:
            return False

        # 地合い悪化時は停止
        if (
            nikkei_velocity is not None
            and nikkei_velocity <= MIN_NIKKEI_VELOCITY
        ):
            return False

        return True

    # --------------------------------------------------------
    # 強制停止（外部要因）
    # --------------------------------------------------------

    def force_stop(self) -> None:
        """
        障害・異常検知時の強制停止
        """
        self.enabled = False