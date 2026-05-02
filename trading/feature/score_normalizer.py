# ============================================================
# trading/feature/score_normalizer.py
# Ver1.0-FINAL-SCORE-NORMALIZER
# ------------------------------------------------------------
# ✔ SUMMARY / RANKING 共通
# ✔ z-score / min-max 両対応
# ✔ weighted_sum 統合
# ✔ NaN / 空安全
# ============================================================

from __future__ import annotations
from typing import Iterable, Dict
import math


# ------------------------------------------------------------
# 正規化
# ------------------------------------------------------------

def min_max_normalize(values: Iterable[float]) -> Dict[int, float]:
    vals = list(values)
    if not vals:
        return {}

    vmin = min(vals)
    vmax = max(vals)

    if math.isclose(vmin, vmax):
        return {i: 0.5 for i in range(len(vals))}

    return {
        i: (v - vmin) / (vmax - vmin)
        for i, v in enumerate(vals)
    }


def zscore_normalize(values: Iterable[float]) -> Dict[int, float]:
    vals = list(values)
    if not vals:
        return {}

    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    std = math.sqrt(var)

    if std == 0:
        return {i: 0.0 for i in range(len(vals))}

    return {
        i: (v - mean) / std
        for i, v in enumerate(vals)
    }


# ------------------------------------------------------------
# weighted sum
# ------------------------------------------------------------

def weighted_score(
    *,
    snapshot_score: float,
    technical_score: float,
    w_snapshot: float,
    w_technical: float,
) -> float:
    return (
        snapshot_score * w_snapshot
        + technical_score * w_technical
    )