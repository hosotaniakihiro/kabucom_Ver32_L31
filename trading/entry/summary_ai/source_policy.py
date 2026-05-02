# ============================================================
# File   : trading/entry/summary_ai/source_policy.py
# Version: PRODUCTION-STABLE-REV1.0
# Purpose:
#   SUMMARY / RANKING_SUMMARY の判定ポリシーを分離する
# ============================================================

from __future__ import annotations

import math
from typing import Any


RANKING_SOURCES = {
    "RANKING",
    "RANKING_SUMMARY",
    "RANKING_SUMMARY_1MIN",
    "RANKING_SUMMARY_3MIN",
    "RANKING_SUMMARY_5MIN",
}

PUSH_SOURCES = {
    "SUMMARY",
    "PUSH",
    "PUSH_SUMMARY",
    "YAHOO",
    "YAHOO_SUMMARY",
}


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def normalize_source(source: str | None) -> str:
    return str(source or "").strip().upper()


def is_ranking_source(source: str | None) -> bool:
    s = normalize_source(source)
    return s in RANKING_SOURCES or "RANKING" in s


def is_push_or_yahoo_source(source: str | None) -> bool:
    s = normalize_source(source)
    return s in PUSH_SOURCES or "PUSH" in s or "YAHOO" in s


def row_passes_pre_ai_filter(
    row,
    *,
    source: str | None,
    min_slope: float = 0.02,
    min_ranking_score: float = 0.0,
    min_ranking_momentum: float = 0.0,
) -> tuple[bool, str]:
    """
    AI gate 前の最低限フィルタ。

    RANKING由来:
      - atr / slope_atr_scaled を使わない
      - ranking_score / ranking_momentum / price_delta_pct を使う

    PUSH/Yahoo由来:
      - slope_atr_scaled を使う
    """

    if is_ranking_source(source):
        ranking_score = to_float(row.get("ranking_score", 0.0))
        ranking_momentum = to_float(row.get("ranking_momentum", 0.0))
        price_delta_pct = to_float(row.get("price_delta_pct", 0.0))
        price_delta = to_float(row.get("price_delta", 0.0))
        rank_improve = to_float(row.get("rank_improve", 0.0))
        volume_delta = to_float(row.get("volume_delta", 0.0))

        ok = (
            ranking_score > min_ranking_score
            or ranking_momentum > min_ranking_momentum
            or price_delta_pct > 0
            or price_delta > 0
            or rank_improve > 0
            or volume_delta > 0
        )

        if not ok:
            return False, (
                "RANKING_SKIP weak "
                f"ranking_score={ranking_score:.3f} "
                f"momentum={ranking_momentum:.3f} "
                f"delta_pct={price_delta_pct:.5f}"
            )

        return True, (
            "RANKING_OK "
            f"ranking_score={ranking_score:.3f} "
            f"momentum={ranking_momentum:.3f} "
            f"delta_pct={price_delta_pct:.5f}"
        )

    slope = to_float(
        row.get(
            "slope_atr_scaled",
            row.get("score_slope", row.get("slope", 0.0)),
        )
    )

    if slope < min_slope:
        return False, f"PUSH_SKIP slope={slope:.4f} < min_slope={min_slope:.4f}"

    return True, f"PUSH_OK slope={slope:.4f}"