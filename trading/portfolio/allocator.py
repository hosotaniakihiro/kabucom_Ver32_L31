# ============================================================
# File   : trading/portfolio/allocator.py
# Version: FINAL-ULTRA-ROBUST-PORTFOLIO-ALLOCATOR
# ------------------------------------------------------------
# ✔ スコアベース配分
# ✔ リスク調整（risk_factor）
# ✔ 最小ポジション制限
# ✔ 最大ポジション制限
# ✔ 総資金制約保証
# ✔ レバレッジ対応
# ✔ ロング/ショート対応
# ✔ ボラティリティ調整配分
# ✔ NaN / inf完全耐性
# ✔ 数値安定化
# ✔ ゼロスコア安全処理
# ============================================================

from __future__ import annotations
import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# メイン配分関数
# ============================================================
def allocate(
    capital: float,
    scores,
    *,
    risk_factor: float = 1.0,
    min_position: float = 0.0,
    max_position: Optional[float] = None,
    allow_short: bool = False,
    leverage: float = 1.0,
    volatility: Optional[np.ndarray] = None,
    vol_targeting: bool = False,
) -> np.ndarray:
    """
    Parameters
    ----------
    capital : float
        総資金
    scores : array-like
        銘柄スコア（正負可）
    risk_factor : float
        リスク倍率
    min_position : float
        最小ポジション閾値
    max_position : float | None
        最大ポジション制限
    allow_short : bool
        ショート許可
    leverage : float
        レバレッジ倍率
    volatility : array-like | None
        銘柄ボラティリティ
    vol_targeting : bool
        ボラティリティ調整有効
    """

    # --------------------------------------------------------
    # 初期チェック
    # --------------------------------------------------------
    if capital <= 0:
        return np.zeros(len(scores))

    scores = np.asarray(scores, dtype=float)
    scores = np.nan_to_num(scores)

    n = len(scores)
    if n == 0:
        return np.array([])

    # --------------------------------------------------------
    # ロング/ショート制御
    # --------------------------------------------------------
    if not allow_short:
        scores = np.maximum(scores, 0)

    # --------------------------------------------------------
    # 全ゼロ処理
    # --------------------------------------------------------
    if np.allclose(scores, 0):
        logger.debug("[ALLOCATOR] all scores zero")
        return np.zeros(n)

    # --------------------------------------------------------
    # ボラティリティターゲティング
    # --------------------------------------------------------
    if vol_targeting and volatility is not None:
        volatility = np.asarray(volatility, dtype=float)
        volatility = np.nan_to_num(volatility)

        volatility = np.where(volatility <= 1e-12, 1.0, volatility)
        scores = scores / volatility

    # --------------------------------------------------------
    # 正規化
    # --------------------------------------------------------
    total_abs = np.sum(np.abs(scores))

    if total_abs <= 1e-12:
        return np.zeros(n)

    weights = scores / total_abs

    # --------------------------------------------------------
    # レバレッジ適用
    # --------------------------------------------------------
    weights = weights * leverage

    # --------------------------------------------------------
    # 資金配分
    # --------------------------------------------------------
    allocation = capital * weights * risk_factor

    # --------------------------------------------------------
    # 最大ポジション制限
    # --------------------------------------------------------
    if max_position is not None:
        allocation = np.clip(allocation, -max_position, max_position)

    # --------------------------------------------------------
    # 最小ポジション制限
    # --------------------------------------------------------
    if min_position > 0:
        allocation = np.where(
            np.abs(allocation) < min_position,
            0.0,
            allocation,
        )

    # --------------------------------------------------------
    # 総資金再正規化（安全）
    # --------------------------------------------------------
    total_alloc = np.sum(np.abs(allocation))
    max_capital = capital * leverage * risk_factor

    if total_alloc > max_capital and total_alloc > 0:
        scale = max_capital / total_alloc
        allocation *= scale

    # --------------------------------------------------------
    # 最終安全処理
    # --------------------------------------------------------
    allocation = np.nan_to_num(allocation)

    logger.debug(
        "[ALLOCATOR] total_alloc=%.2f leverage=%.2f",
        np.sum(np.abs(allocation)),
        leverage,
    )

    return allocation


# ============================================================
# ヘルパー：リスク均等配分
# ============================================================
def risk_parity_allocation(
    capital: float,
    volatility: np.ndarray,
) -> np.ndarray:
    """
    ボラティリティ逆数で配分
    """

    volatility = np.asarray(volatility, dtype=float)
    volatility = np.nan_to_num(volatility)

    volatility = np.where(volatility <= 1e-12, 1.0, volatility)

    inv_vol = 1 / volatility

    weights = inv_vol / np.sum(inv_vol)

    return capital * weights


# ============================================================
# ヘルパー：均等配分
# ============================================================
def equal_weight_allocation(
    capital: float,
    n_assets: int,
) -> np.ndarray:

    if n_assets <= 0:
        return np.array([])

    return np.ones(n_assets) * (capital / n_assets)