# ============================================================
# File: AI/orchestrator_buy_sell_tonosama.py
# ------------------------------------------------------------
# 殿様イナゴ BUY / SELL 統合オーケストレーター
#
# ✔ BUY / SELL を同一口座で同時制御
# ✔ 日次損失・連敗・時間帯で自動停止
# ✔ 市場地合い悪化時は両側停止
# ✔ ENTRY ロジック・AI とは完全独立
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Optional


# ============================================================
# 固定パラメータ（絶対に変更しない）
# ============================================================

# 日次最大損失（口座ベース）
MAX_DAILY_LOSS = -0.03          # -3%

# BUY / SELL 合算の連敗
MAX_CONSECUTIVE_LOSSES = 4

# 新規 ENTRY 停止時刻
NO_ENTRY_AFTER = dt.time(14, 50)

# 地合い悪化（指数）
MIN_NIKKEI_VELOCITY = -0.002    # -0.2% / min

# SELL 側を先に止める損失ライン
SELL_EARLY_STOP_LOSS = -0.015   # -1.5%


# ============================================================
# Orchestrator
# ============================================================

class BuySellTonosamaOrchestrator:
    """
    BUY / SELL 殿様を統合管理する最上位司令塔
    """

    def __init__(self) -> None:
        self.enabled_buy: bool = True
        self.enabled_sell: bool = True

        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0

        self.last_trade_date: Optional[dt.date] = None

    # --------------------------------------------------------
    # 日付リセット
    # --------------------------------------------------------

    def _ensure_new_day(self, now: dt.datetime) -> None:
        today = now.date()
        if self.last_trade_date != today:
            self.enabled_buy = True
            self.enabled_sell = True
            self.daily_pnl = 0.0
            self.consecutive_losses = 0
            self.last_trade_date = today

    # --------------------------------------------------------
    # トレード結果通知
    # --------------------------------------------------------

    def on_trade_result(
        self,
        *,
        side: str,
        pnl: float,
        now: dt.datetime,
    ) -> None:
        """
        1トレード終了時に必ず呼ぶ

        Parameters
        ----------
        side : str
            "BUY" or "SELL"
        pnl : float
            トレード損益率
        now : datetime
        """

        self._ensure_new_day(now)

        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        # SELL は先に止める（踏み上げ事故防止）
        if self.daily_pnl <= SELL_EARLY_STOP_LOSS:
            self.enabled_sell = False

        # 連敗停止（両側）
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.enabled_buy = False
            self.enabled_sell = False

        # 日次損失停止（全停止）
        if self.daily_pnl <= MAX_DAILY_LOSS:
            self.enabled_buy = False
            self.enabled_sell = False

    # --------------------------------------------------------
    # ENTRY 可否判定
    # --------------------------------------------------------

    def allow_entry(
        self,
        *,
        side: str,
        now: dt.datetime,
        nikkei_velocity: Optional[float] = None,
    ) -> bool:
        """
        BUY / SELL 共通 ENTRY 判定

        Parameters
        ----------
        side : str
            "BUY" or "SELL"
        now : datetime
        nikkei_velocity : float, optional

        Returns
        -------
        bool
        """

        self._ensure_new_day(now)

        # 時間帯ガード
        if now.time() >= NO_ENTRY_AFTER:
            return False

        # 地合い悪化（両側停止）
        if (
            nikkei_velocity is not None
            and nikkei_velocity <= MIN_NIKKEI_VELOCITY
        ):
            return False

        if side == "BUY":
            return self.enabled_buy

        if side == "SELL":
            return self.enabled_sell

        return False

    # --------------------------------------------------------
    # 強制停止
    # --------------------------------------------------------

    def force_stop(self) -> None:
        """
        障害・異常時の全停止
        """
        self.enabled_buy = False
        self.enabled_sell = False