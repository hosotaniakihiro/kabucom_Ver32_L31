# -*- coding: utf-8 -*-
"""
Patch Summary-AI RANKING pre-filter so ranking-derived candidates are not
incorrectly dropped when ranking_score/ranking_momentum columns are all zero.

This is not a fail-open rescue. It only uses already-existing ranking signal
columns such as display_score/score_buy/score_sell/best_rank/rank_type and
ranking movement columns when present. If no ranking signal exists, candidates
still stay blocked.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-RANKING-PREFILTER-SIGNAL-COLUMNS"
_INSTALLED = False
_ORIG_APPLY_RANKING_PRE_FILTER = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _num_series(df: Any, col: str):
    import pandas as pd
    if col not in df.columns:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(0.0)


def _str_series(df: Any, col: str):
    import pandas as pd
    if col not in df.columns:
        return pd.Series("", index=df.index)
    return df[col].astype(str).fillna("").str.strip()


def _patched_apply_ranking_pre_filter(
    df,
    *,
    source: str,
    interval: int | str,
    enabled: bool = True,
    min_ranking_score: float | None = None,
    min_ranking_momentum: float | None = None,
):
    import pandas as pd

    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    if not enabled:
        return df

    out = df.copy()
    min_score = float(min_ranking_score) if min_ranking_score is not None else _env_float("RANKING_AI_MIN_SCORE", 0.0)
    min_mom = float(min_ranking_momentum) if min_ranking_momentum is not None else _env_float("RANKING_AI_MIN_MOMENTUM", 0.0)

    # Native expected columns.
    ranking_score = _num_series(out, "ranking_score")
    ranking_momentum = _num_series(out, "ranking_momentum")
    price_delta_pct = _num_series(out, "price_delta_pct")
    price_delta = _num_series(out, "price_delta")
    rank_improve = _num_series(out, "rank_improve")
    volume_delta = _num_series(out, "volume_delta")

    # Existing score columns produced by ranking summary / bridge patches.
    score_cols = (
        "score_buy", "buy_score", "score_sell", "sell_score", "score_total",
        "final_score", "display_score", "score", "pending_score", "priority",
        "score_mtf", "mtf_score", "rank_strength", "summary_ai_priority",
    )
    score_signal = pd.Series(0.0, index=out.index)
    present_score_cols: list[str] = []
    for col in score_cols:
        if col in out.columns:
            present_score_cols.append(col)
            score_signal = pd.concat([score_signal, _num_series(out, col).abs()], axis=1).max(axis=1)

    # Ranking list itself is also a signal, but only for known ranking rows.
    rank_cols = ("best_rank", "rank", "ranking_rank", "rank_no", "display_rank")
    best_rank_signal = pd.Series(False, index=out.index)
    present_rank_cols: list[str] = []
    max_rank = int(max(1, _env_float("RANKING_AI_PREFILTER_MAX_BEST_RANK", 20.0)))
    for col in rank_cols:
        if col in out.columns:
            present_rank_cols.append(col)
            rv = _num_series(out, col)
            best_rank_signal = best_rank_signal | ((rv > 0) & (rv <= max_rank))

    rank_type = _str_series(out, "rank_type") if "rank_type" in out.columns else _str_series(out, "ranking_type")
    ranking_type_signal = rank_type.ne("") & ~rank_type.str.lower().isin({"nan", "none", "<na>", "0"})

    range_signal = pd.Series(False, index=out.index)
    for col in ("range_pct", "intraday_range_pct", "day_range_pct", "_intrabar_range_pct", "_max_price_change_pct"):
        if col in out.columns:
            rv = _num_series(out, col).abs()
            rv = rv.where(rv <= 1.0, rv / 100.0)
            range_signal = range_signal | (rv >= _env_float("RANKING_AI_PREFILTER_MIN_RANGE_PCT", 0.0015))

    volume_signal = pd.Series(False, index=out.index)
    for col in ("volume_surge_ratio", "volume_surge_ratio_1m", "volume_surge_ratio_3m", "volume_surge_ratio_5m"):
        if col in out.columns:
            volume_signal = volume_signal | (_num_series(out, col) >= _env_float("RANKING_AI_PREFILTER_MIN_SURGE_RATIO", 1.2))

    mask = (
        (ranking_score > min_score)
        | (ranking_momentum > min_mom)
        | (price_delta_pct > 0)
        | (price_delta > 0)
        | (rank_improve > 0)
        | (volume_delta > 0)
        | (score_signal > min_score)
        | best_rank_signal
        | (ranking_type_signal & (range_signal | volume_signal | (score_signal > 0)))
    )

    before = len(out)
    skipped_df = out.loc[~mask].copy()
    out = out.loc[mask].copy()

    try:
        skipped_head = skipped_df[[c for c in ("symbol", "ranking_score", "ranking_momentum", "display_score", "score_buy", "score_sell", "best_rank", "rank_type", "range_pct") if c in skipped_df.columns]].head(20).to_dict(orient="records")
    except Exception:
        skipped_head = []

    logger.warning(
        "[SUMMARY AI RANKING PREFILTER SIGNAL] result source=%s interval=%s before=%s after=%s skipped=%s min_score=%.4f min_mom=%.4f score_cols=%s rank_cols=%s max_rank=%s version=%s skipped_head=%s",
        source, interval, before, len(out), before - len(out), min_score, min_mom,
        present_score_cols, present_rank_cols, max_rank, VERSION, skipped_head,
    )
    return out


def install() -> bool:
    global _INSTALLED, _ORIG_APPLY_RANKING_PRE_FILTER
    if _INSTALLED:
        return True
    try:
        from trading.entry.summary_ai import runner
        cur = getattr(runner, "_apply_ranking_pre_filter", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_ranking_prefilter_signal_v1", False):
            _INSTALLED = True
            return True
        _ORIG_APPLY_RANKING_PRE_FILTER = cur
        _patched_apply_ranking_pre_filter._summary_ai_ranking_prefilter_signal_v1 = True  # type: ignore[attr-defined]
        _patched_apply_ranking_pre_filter._original = cur  # type: ignore[attr-defined]
        runner._apply_ranking_pre_filter = _patched_apply_ranking_pre_filter
        _INSTALLED = True
        logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] installed version=%s original=%s", VERSION, getattr(cur, "__name__", type(cur).__name__))
        return True
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] auto install failed")


__all__ = ["install", "VERSION"]
