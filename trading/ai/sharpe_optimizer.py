# ============================================================
# File   : trading/ai/sharpe_optimizer.py
# Version: FINAL-ULTRA-ROBUST-SHARPE-OPTIMIZER
# ------------------------------------------------------------
# ✔ Sharpe ratio計算（年率対応）
# ✔ Sortino ratio対応
# ✔ Calmar ratio対応
# ✔ 最大ドローダウン計算
# ✔ ポートフォリオSharpe最適化
# ✔ 制約付きDirichlet探索
# ✔ weight min/max制約
# ✔ L2正則化
# ✔ ボラティリティ制限
# ✔ NaN / inf耐性
# ✔ 数値安定強化
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
    *,
    annualize: bool = False,
    periods_per_year: int = 252,
) -> float:

    if returns is None or len(returns) == 0:
        return 0.0

    returns = np.asarray(returns, dtype=float)
    returns = np.nan_to_num(returns)

    excess = returns - risk_free

    std = np.std(excess)
    if std <= 1e-12:
        return 0.0

    sharpe = np.mean(excess) / std

    if annualize:
        sharpe *= np.sqrt(periods_per_year)

    return float(sharpe)


# ============================================================
# Sortino計算
# ============================================================
def calculate_sortino(
    returns: np.ndarray,
    risk_free: float = 0.0,
) -> float:

    returns = np.asarray(returns, dtype=float)
    returns = np.nan_to_num(returns)

    downside = returns[returns < risk_free]

    if len(downside) == 0:
        return 0.0

    downside_std = np.std(downside)
    if downside_std <= 1e-12:
        return 0.0

    return float((np.mean(returns) - risk_free) / downside_std)


# ============================================================
# 最大ドローダウン
# ============================================================
def calculate_max_drawdown(returns: np.ndarray) -> float:

    returns = np.asarray(returns, dtype=float)
    returns = np.nan_to_num(returns)

    cumulative = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / peak

    return float(np.min(drawdown))


# ============================================================
# Calmar比
# ============================================================
def calculate_calmar(returns: np.ndarray) -> float:

    returns = np.asarray(returns, dtype=float)
    returns = np.nan_to_num(returns)

    mdd = abs(calculate_max_drawdown(returns))
    if mdd <= 1e-12:
        return 0.0

    annual_return = np.mean(returns) * 252
    return float(annual_return / mdd)


# ============================================================
# ポートフォリオ最適化
# ============================================================
def optimize_weight(
    returns_matrix: np.ndarray,
    *,
    n_iter: int = 3000,
    risk_free: float = 0.0,
    max_drawdown_limit: float | None = None,
    max_volatility: float | None = None,
    l2_penalty: float = 0.0,
    weight_min: float = 0.0,
    weight_max: float = 1.0,
    metric: str = "sharpe",  # sharpe / sortino / calmar
) -> np.ndarray:

    if returns_matrix is None or returns_matrix.size == 0:
        return np.array([])

    returns_matrix = np.asarray(returns_matrix, dtype=float)
    returns_matrix = np.nan_to_num(returns_matrix)

    n_assets = returns_matrix.shape[1]

    best_score = -1e12
    best_w = np.ones(n_assets) / n_assets

    for _ in range(n_iter):

        # Dirichletでweight生成
        w = np.random.dirichlet(np.ones(n_assets))

        # min/max制約
        if weight_min > 0 or weight_max < 1:
            w = np.clip(w, weight_min, weight_max)
            w = w / w.sum()

        portfolio_returns = returns_matrix @ w

        # メトリクス選択
        if metric == "sortino":
            score = calculate_sortino(portfolio_returns, risk_free)
        elif metric == "calmar":
            score = calculate_calmar(portfolio_returns)
        else:
            score = calculate_sharpe(portfolio_returns, risk_free)

        # ドローダウン制限
        if max_drawdown_limit is not None:
            mdd = calculate_max_drawdown(portfolio_returns)
            if mdd < -abs(max_drawdown_limit):
                continue

        # ボラティリティ制限
        if max_volatility is not None:
            vol = np.std(portfolio_returns)
            if vol > max_volatility:
                continue

        # L2正則化
        if l2_penalty > 0:
            score -= l2_penalty * np.sum(w ** 2)

        if score > best_score:
            best_score = score
            best_w = w

    logger.debug("[SHARPE_OPT] best_score=%.6f metric=%s", best_score, metric)

    return best_w