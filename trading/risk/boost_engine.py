# ============================================================
# File   : trading/risk/boost_engine.py
# Version: Ver28-PRODUCTION-BOOST-HARDENED-FINAL
# ------------------------------------------------------------
# ✔ Ver27完全保持（削除ゼロ）
# ✔ NaN/inf完全防御
# ✔ 型安全
# ✔ ヒステリシス維持
# ✔ 連敗即解除
# ✔ DD安全装置
# ✔ Regime変化解除
# ✔ deterministic
# ✔ 状態可視化API追加
# ✔ 将来AI拡張対応
# ✔ 本番例外耐性MAX
# ============================================================

from __future__ import annotations

import logging
import time
import math
from threading import Lock

logger = logging.getLogger(__name__)


# ============================================================
# SAFE
# ============================================================

def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


# ============================================================
# BOOST ENGINE
# ============================================================

class BoostEngine:

    # --------------------------------------------------------
    # 初期設定（既存保持）
    # --------------------------------------------------------

    ACTIVATION_SCORE_THRESHOLD = 3
    DEACTIVATION_SCORE_THRESHOLD = 2

    MIN_ACTIVE_DURATION_SEC = 300     # 5分維持
    MAX_DRAWDOWN_LIMIT = -0.05        # -5%で強制解除
    MAX_CONSECUTIVE_LOSSES = 2

    # --------------------------------------------------------

    def __init__(self):

        self.active: bool = False
        self._last_activation_time: float = 0.0
        self._last_regime: int | None = None
        self._last_activation_score: int = 0
        self._last_deactivation_score: int = 0

        self._lock = Lock()

    # --------------------------------------------------------
    # 内部スコア計算
    # --------------------------------------------------------

    def _calc_activation_score(
        self,
        win_rate: float,
        regime: int,
        drawdown: float,
        collapse_prob: float,
    ) -> int:

        score = 0

        if win_rate > 0.6:
            score += 1

        if regime == 1:  # トレンド相場
            score += 1

        if drawdown > -0.03:
            score += 1

        if collapse_prob < 0.3:
            score += 1

        return score

    def _calc_deactivation_score(
        self,
        win_rate: float,
        collapse_prob: float,
    ) -> int:

        score = 0

        if win_rate < 0.5:
            score += 1

        if collapse_prob > 0.5:
            score += 1

        return score

    # --------------------------------------------------------
    # メイン更新ロジック
    # --------------------------------------------------------

    def update(
        self,
        win_rate: float,
        regime: int,
        drawdown: float,
        collapse_prob: float,
        consecutive_losses: int,
        regime_changed: bool,
    ) -> bool:

        with self._lock:

            try:

                win_rate = _safe_float(win_rate)
                regime = _safe_int(regime)
                drawdown = _safe_float(drawdown)
                collapse_prob = _safe_float(collapse_prob)
                consecutive_losses = _safe_int(consecutive_losses)

                now = time.time()

                # ==================================================
                # 強制解除条件
                # ==================================================

                if drawdown < self.MAX_DRAWDOWN_LIMIT:
                    if self.active:
                        logger.warning("🚨 BOOST OFF (Max Drawdown)")
                    self.active = False
                    return self.active

                if consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
                    if self.active:
                        logger.warning("🚨 BOOST OFF (Consecutive Losses)")
                    self.active = False
                    return self.active

                if regime_changed:
                    if self.active:
                        logger.info("🔄 BOOST OFF (Regime Changed)")
                    self.active = False
                    return self.active

                # ==================================================
                # 未発動 → 発動判定
                # ==================================================

                if not self.active:

                    activation_score = self._calc_activation_score(
                        win_rate,
                        regime,
                        drawdown,
                        collapse_prob,
                    )

                    self._last_activation_score = activation_score

                    if activation_score >= self.ACTIVATION_SCORE_THRESHOLD:
                        self.active = True
                        self._last_activation_time = now
                        self._last_regime = regime

                        logger.info(
                            "🚀 BOOST MODE ACTIVATED score=%s",
                            activation_score,
                        )

                    return self.active

                # ==================================================
                # 発動中 → 維持 / 解除
                # ==================================================

                # ヒステリシス（最低維持時間）
                if now - self._last_activation_time < self.MIN_ACTIVE_DURATION_SEC:
                    return self.active

                deactivation_score = self._calc_deactivation_score(
                    win_rate,
                    collapse_prob,
                )

                self._last_deactivation_score = deactivation_score

                if deactivation_score >= self.DEACTIVATION_SCORE_THRESHOLD:
                    self.active = False
                    logger.info(
                        "🛑 BOOST MODE DEACTIVATED score=%s",
                        deactivation_score,
                    )
                    return self.active

                return self.active

            except Exception:
                logger.exception("[BOOST_UPDATE_FATAL]")
                return self.active

    # --------------------------------------------------------
    # 状態取得
    # --------------------------------------------------------

    def is_active(self) -> bool:
        return self.active

    def get_state(self) -> dict:
        return {
            "active": self.active,
            "last_activation_time": self._last_activation_time,
            "last_regime": self._last_regime,
            "activation_score": self._last_activation_score,
            "deactivation_score": self._last_deactivation_score,
        }

    def reset(self):
        with self._lock:
            self.active = False
            self._last_activation_time = 0.0
            self._last_regime = None
            self._last_activation_score = 0
            self._last_deactivation_score = 0
            logger.info("🔄 BOOST RESET")