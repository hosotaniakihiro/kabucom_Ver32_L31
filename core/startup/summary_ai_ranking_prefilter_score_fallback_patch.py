# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_ranking_prefilter_score_fallback_patch.py
# Version: V1-RANKING-PREFILTER-SCORE-FALLBACK
# ------------------------------------------------------------
# RANKING source summary rows can have ranking_score/ranking_momentum all zero
# while score_buy/score_sell/score_mtf/slope-derived scores are already present.
# In that case runner._apply_ranking_pre_filter removed every row before AI could
# evaluate candidates. This patch bridges existing score columns into ranking_*.
# It does not fail-open empty/low-score rows; it only uses already computed scores.
# ============================================================
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-RANKING-PREFILTER-SCORE-FALLBACK"
_INSTALLED = False


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _series(df, name: str, default: float = 0.0):
    import pandas as pd
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce").fillna(default)
    return pd.Series([default] * len(df), index=df.index, dtype="float64")


def _max_series(*items):
    import pandas as pd
    if not items:
        return pd.Series(dtype="float64")
    out = items[0].copy()
    for s in items[1:]:
        out = out.combine(s, max)
    return out.fillna(0.0)


def _prepare_ranking_scores(df):
    if df is None or getattr(df, "empty", True):
        return df
    out = df.copy()

    score = _series(out, "score")
    score_total = _series(out, "score_total")
    final_score = _series(out, "final_score")
    display_score = _series(out, "display_score")
    score_buy = _series(out, "score_buy")
    buy_score = _series(out, "buy_score")
    score_sell = _series(out, "score_sell")
    sell_score = _series(out, "sell_score")
    score_mtf = _max_series(_series(out, "score_mtf"), _series(out, "mtf_score"), _series(out, "mtf"))
    score_slope = _series(out, "score_slope")
    slope_scaled = _series(out, "slope_atr_scaled")

    pos_base = _max_series(score.clip(lower=0), score_total.clip(lower=0), final_score.clip(lower=0), display_score.clip(lower=0))
    neg_base = _max_series((-score).clip(lower=0), (-score_total).clip(lower=0), (-final_score).clip(lower=0), (-display_score).clip(lower=0))

    buy_derived = _max_series(score_buy, buy_score, pos_base, score_mtf.clip(lower=0), score_slope.clip(lower=0), slope_scaled.clip(lower=0))
    sell_derived = _max_series(score_sell, sell_score, neg_base, (-score_slope).clip(lower=0), (-slope_scaled).clip(lower=0))
    effective = _max_series(buy_derived, sell_derived)

    for dst, src in (("score_buy", buy_derived), ("buy_score", buy_derived), ("score_sell", sell_derived), ("sell_score", sell_derived)):
        cur = _series(out, dst)
        out[dst] = cur.where(cur > 0, src)

    ranking_score = _series(out, "ranking_score")
    ranking_momentum = _series(out, "ranking_momentum")
    out["ranking_score"] = ranking_score.where(ranking_score > 0, effective)
    out["ranking_momentum"] = ranking_momentum.where(ranking_momentum > 0, _max_series(score_slope.abs(), slope_scaled.abs()))

    for c in ("price_delta_pct", "price_delta", "rank_improve", "volume_delta"):
        if c not in out.columns:
            out[c] = 0.0

    try:
        logger.warning(
            "[SUMMARY AI RANKING PREFILTER FALLBACK] prepared rows=%s effective_pos=%s buy_pos=%s sell_pos=%s rank_pos=%s version=%s",
            len(out), int((effective > 0).sum()), int((out["score_buy"] > 0).sum()), int((out["score_sell"] > 0).sum()), int((out["ranking_score"] > 0).sum()), VERSION,
        )
    except Exception:
        pass
    return out


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry.summary_ai.runner as runner
        cur = getattr(runner, "_apply_ranking_pre_filter", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI RANKING PREFILTER FALLBACK] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_ranking_prefilter_score_fallback_v1", False):
            _INSTALLED = True
            return True
        orig = getattr(cur, "_original", cur)

        def _patched_apply_ranking_pre_filter(df, *args, **kwargs):
            prepared = _prepare_ranking_scores(df)
            out = orig(prepared, *args, **kwargs)
            if (out is None or getattr(out, "empty", True)) and prepared is not None and not getattr(prepared, "empty", True):
                try:
                    import pandas as pd
                    rank_score = pd.to_numeric(prepared.get("ranking_score", 0.0), errors="coerce").fillna(0.0)
                    if (rank_score > 0).any():
                        fallback = prepared[rank_score > 0].copy()
                        logger.warning("[SUMMARY AI RANKING PREFILTER FALLBACK] original emptied rows; fallback rows=%s/%s version=%s", len(fallback), len(prepared), VERSION)
                        return fallback
                except Exception:
                    logger.exception("[SUMMARY AI RANKING PREFILTER FALLBACK] fallback filter failed")
            return out

        _patched_apply_ranking_pre_filter._summary_ai_ranking_prefilter_score_fallback_v1 = True  # type: ignore[attr-defined]
        _patched_apply_ranking_pre_filter._original = orig  # type: ignore[attr-defined]
        runner._apply_ranking_pre_filter = _patched_apply_ranking_pre_filter
        _INSTALLED = True
        logger.warning("[SUMMARY AI RANKING PREFILTER FALLBACK] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER FALLBACK] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RANKING PREFILTER FALLBACK] auto install failed")


__all__ = ["install", "VERSION"]
