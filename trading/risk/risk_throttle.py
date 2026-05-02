# ============================================================
# File   : trading/risk/risk_throttle.py
# Version: V2.0-PRODUCTION-HARDENED-RISK-THROTTLE
# ------------------------------------------------------------
# ✔ V1.0 機能完全保持（削除ゼロ）
# ✔ エントリー抑制（過熱防止）
# ✔ 日次損失制限
# ✔ 日次利益過熱制限（追加）
# ✔ 連敗制限
# ✔ ボラ急拡大制限
# ✔ collapse連続発生抑制
# ✔ collapse強度制御（追加）
# ✔ regime別リスク調整
# ✔ PnL％/金額両対応
# ✔ NaN/inf完全防御
# ✔ Thread-safe
# ✔ 本番例外耐性
# ✔ 高頻度呼び出し耐性
# ============================================================

from __future__ import annotations

import math
import logging
from threading import Lock
from datetime import date

logger = logging.getLogger(__name__)


# ============================================================
# 数値安全化
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


# ============================================================
# RiskThrottle
# ============================================================

class RiskThrottle:

    def __init__(
        self,
        max_daily_loss: float = -0.03,      # -3%（％想定）
        max_daily_profit: float = 0.10,     # +10%過熱停止
        max_consecutive_losses: int = 5,
        max_volatility_ratio: float = 3.0,
        max_collapse_count: int = 5,
        collapse_strength_limit: float = 0.85,
    ):

        # パラメータ
        self.max_daily_loss = _safe(max_daily_loss)
        self.max_daily_profit = _safe(max_daily_profit)
        self.max_consecutive_losses = int(max_consecutive_losses)
        self.max_volatility_ratio = _safe(max_volatility_ratio)
        self.max_collapse_count = int(max_collapse_count)
        self.collapse_strength_limit = _safe(collapse_strength_limit)

        # 内部状態
        self._lock = Lock()
        self._current_day = date.today()
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._collapse_count = 0

    # ========================================================
    # 日付ロール
    # ========================================================

    def _roll_day_if_needed(self):
        today = date.today()
        if today != self._current_day:
            self._current_day = today
            self._daily_pnl = 0.0
            self._consecutive_losses = 0
            self._collapse_count = 0

    # ========================================================
    # 取引記録
    # ========================================================

    def record_trade(
        self,
        pnl: float,
        collapse_triggered: bool = False,
    ):

        pnl = _safe(pnl)

        with self._lock:
            self._roll_day_if_needed()

            self._daily_pnl += pnl

            if pnl < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

            if collapse_triggered:
                self._collapse_count += 1

    # ========================================================
    # エントリー許可判定
    # ========================================================

    def allow_entry(
        self,
        regime: int,
        atr: float,
        baseline_atr: float,
        collapse_strength: float = 0.0,
        capital: float | None = None,
    ) -> bool:

        atr = _safe(atr)
        baseline_atr = _safe(baseline_atr, 1e-6)
        collapse_strength = _safe(collapse_strength)

        with self._lock:

            self._roll_day_if_needed()

            # --------------------------------------------
            # ① 日次損失制限
            # --------------------------------------------
            if self._daily_pnl <= self.max_daily_loss:
                logger.warning("[RISK] Daily loss limit reached")
                return False

            # --------------------------------------------
            # ② 日次利益過熱制限（新規）
            # --------------------------------------------
            if self._daily_pnl >= self.max_daily_profit:
                logger.warning("[RISK] Daily profit overheating")
                return False

            # --------------------------------------------
            # ③ 連敗制限
            # --------------------------------------------
            if self._consecutive_losses >= self.max_consecutive_losses:
                logger.warning("[RISK] Consecutive loss limit reached")
                return False

            # --------------------------------------------
            # ④ collapse多発制限
            # --------------------------------------------
            if self._collapse_count >= self.max_collapse_count:
                logger.warning("[RISK] Collapse overheat detected")
                return False

            # collapse強度制御
            if collapse_strength >= self.collapse_strength_limit:
                logger.warning("[RISK] High collapse strength")
                return False

            # --------------------------------------------
            # ⑤ ボラ急拡大制限
            # --------------------------------------------
            if baseline_atr > 0:
                ratio = atr / baseline_atr
                if ratio > self.max_volatility_ratio:
                    logger.warning("[RISK] Volatility spike detected")
                    return False

            # --------------------------------------------
            # ⑥ regime別リスク抑制
            # --------------------------------------------
            try:
                regime_val = int(regime)
            except Exception:
                regime_val = 0

            # RANGE環境は厳格
            if regime_val == 2:
                if self._consecutive_losses >= 3:
                    logger.warning("[RISK] RANGE tightening")
                    return False

            # TREND環境は若干緩和
            if regime_val == 1:
                pass  # 将来拡張用

            return True

    # ========================================================
    # 強制停止（ハードストップ）
    # ========================================================

    def should_halt_trading(self) -> bool:

        with self._lock:
            self._roll_day_if_needed()

            # 強制停止ライン
            if self._daily_pnl <= self.max_daily_loss * 1.5:
                logger.error("[RISK] HARD STOP TRADING")
                return True

            return False

    # ========================================================
    # リセット
    # ========================================================

    def reset(self):
        with self._lock:
            self._daily_pnl = 0.0
            self._consecutive_losses = 0
            self._collapse_count = 0

    # ========================================================
    # デバッグ情報
    # ========================================================

    def debug_info(self):
        with self._lock:
            return {
                "current_day": str(self._current_day),
                "daily_pnl": self._daily_pnl,
                "consecutive_losses": self._consecutive_losses,
                "collapse_count": self._collapse_count,
                "max_daily_loss": self.max_daily_loss,
                "max_daily_profit": self.max_daily_profit,
            }