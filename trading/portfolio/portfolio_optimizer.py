# ============================================================
# File   : trading/portfolio/portfolio_optimizer.py
# Version: FINAL-PORTFOLIO-OPTIMIZER-V1
# ------------------------------------------------------------
# ✔ リスクパリティ型
# ✔ MTF強度加味
# ✔ 指数補正統合
# ✔ 正規化
# ✔ 最大ウェイト制限
# ============================================================

from typing import Dict
import numpy as np


def risk_weight(volatility: float) -> float:
    return 1 / (volatility + 1e-6)


def optimize_weights(
    candidates: Dict[str, dict],
    *,
    max_weight: float = 0.3,
):
    """
    candidates:
        {
            symbol: {
                "volatility": float,
                "mtf_strength": float,
                "index_mult": float,
            }
        }
    """

    raw_weights = {}

    for sym, data in candidates.items():
        vol = data.get("volatility", 1.0)
        mtf = abs(data.get("mtf_strength", 0))
        idx = data.get("index_mult", 1.0)

        weight = risk_weight(vol)
        weight *= (1 + mtf)
        weight *= idx

        raw_weights[sym] = weight

    total = sum(raw_weights.values())
    if total == 0:
        return {k: 0 for k in raw_weights}

    # 正規化
    norm = {k: v / total for k, v in raw_weights.items()}

    # 上限制限
    clipped = {k: min(v, max_weight) for k, v in norm.items()}

    # 再正規化
    total2 = sum(clipped.values())
    return {k: v / total2 for k, v in clipped.items()}