# ============================================================
# File   : trading/risk/position_sizer.py
# Version: V2.0-FINAL-ROBUST-POSITION-SIZER-PRODUCTION
# ------------------------------------------------------------
# ✔ V1機能完全保持（削除ゼロ）
# ✔ ATRベース
# ✔ capital割合
# ✔ 最小ロット制御
# ✔ NaN/inf完全耐性
# ✔ ATRゼロ安全回避
# ✔ 最大サイズ制御（過剰レバ防止）
# ✔ 数値安定化
# ✔ 本番例外耐性
# ✔ ログ強化
# ============================================================

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


# ============================================================
# 数値安全化
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
# メイン計算
# ============================================================

def calculate_position_size(
    capital: float,
    risk_per_trade: float,
    atr: float | None,
    atr_multiplier: float = 2.0,
    min_size: int = 1,
    max_size: int | None = None,
) -> int:
    """
    ATRベースのポジションサイズ計算

    Parameters
    ----------
    capital : float
        総資金
    risk_per_trade : float
        1トレードあたりのリスク割合（例: 0.01 = 1%）
    atr : float
        ATR値
    atr_multiplier : float
        ストップ距離倍率
    min_size : int
        最小ロット
    max_size : int | None
        最大ロット（Noneなら無制限）

    Returns
    -------
    int : 推奨ポジションサイズ
    """

    try:

        capital = _safe(capital)
        risk_per_trade = _safe(risk_per_trade)
        atr = _safe(atr)
        atr_multiplier = _safe(atr_multiplier, 1.0)

        # ----------------------------------------------------
        # 基本安全チェック
        # ----------------------------------------------------
        if capital <= 0:
            logger.warning("[POSITION_SIZER] capital <= 0")
            return 0

        if risk_per_trade <= 0:
            logger.warning("[POSITION_SIZER] risk_per_trade <= 0")
            return 0

        if atr <= 0:
            logger.warning("[POSITION_SIZER] ATR invalid")
            return 0

        if atr_multiplier <= 0:
            atr_multiplier = 1.0

        # ----------------------------------------------------
        # リスク計算
        # ----------------------------------------------------
        risk_amount = capital * risk_per_trade
        stop_distance = atr * atr_multiplier

        if stop_distance <= 0:
            logger.warning("[POSITION_SIZER] stop_distance invalid")
            return 0

        raw_size = risk_amount / stop_distance

        raw_size = _safe(raw_size)

        if raw_size <= 0:
            return 0

        size = int(raw_size)

        # ----------------------------------------------------
        # 最小ロット保証
        # ----------------------------------------------------
        size = max(size, int(min_size))

        # ----------------------------------------------------
        # 最大ロット制限（過剰レバ防止）
        # ----------------------------------------------------
        if max_size is not None:
            try:
                max_size = int(max_size)
                if max_size > 0:
                    size = min(size, max_size)
            except Exception:
                logger.exception("[POSITION_SIZER] max_size invalid")

        return size

    except Exception:
        logger.exception("[POSITION_SIZER] fatal error")
        return 0