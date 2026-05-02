# ============================================================
# File   : trading/ai/sharpe_optimizer.py
# Version: FINAL-ROBUST-SHARPE-OPTIMIZER
# ------------------------------------------------------------
# ✔ Sharpe ratio計算（数値安定）
# ✔ リスクフリー率対応
# ✔ 最大ドローダウン計算
# ✔ ポートフォリオSharpe最適化
# ✔ 制約付きDirichlet探索
# ✔ NaN / inf耐性
# ============================================================

from __future__ import annotations
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ============================================================
# Sharpe計算
# ============================================================
def calculate_sharpe(
    returns: np.ndarray,
    risk_free: float = 0.0,
) -> float:

    if returns is None or len(returns) == 0:
        return 0.0

    returns = np.asarray(returns, dtype=float)

    if np.isnan(returns).all():
        return 0.0

    excess = returns - risk_free

    std = np.std(excess)

    if std == 0 or np.isnan(std):
        return 0.0

    return float(np.mean(excess) / std)


# ============================================================
# 最大ドローダウン
# ============================================================
def calculate_max_drawdown(returns: np.ndarray) -> float:

    if returns is None or len(returns) == 0:
        return 0.0

    cumulative = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / peak

    return float(np.min(drawdown))


# ============================================================
# ポートフォリオSharpe最適化
# ============================================================
def optimize_weight(
    returns_matrix: np.ndarray,
    *,
    n_iter: int = 2000,
    risk_free: float = 0.0,
    max_drawdown_limit: float | None = None,
) -> np.ndarray:

    if returns_matrix is None or returns_matrix.size == 0:
        return np.array([])

    returns_matrix = np.asarray(returns_matrix, dtype=float)

    n_assets = returns_matrix.shape[1]

    best_sharpe = -9999
    best_w = np.ones(n_assets) / n_assets

    for _ in range(n_iter):

        w = np.random.dirichlet(np.ones(n_assets))

        portfolio_returns = returns_matrix @ w

        sharpe = calculate_sharpe(portfolio_returns, risk_free)

        if max_drawdown_limit is not None:
            mdd = calculate_max_drawdown(portfolio_returns)
            if mdd < -abs(max_drawdown_limit):
                continue

        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_w = w

    logger.debug("[SHARPE_OPT] best_sharpe=%.4f", best_sharpe)

    return best_w