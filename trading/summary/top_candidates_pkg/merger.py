# ============================================================
# File   : trading/summary/top_candidates_pkg/merger.py
# Version: Ver2.2-PRODUCTION-SUMMARY-TOP-CANDIDATES-MERGER
# ------------------------------------------------------------
# Function:
#   - PUSH由来 + ランキング由来候補を AI-Gate 用に統合
#   - matched_sources / has_push_summary / has_ranking_summary を付与
#   - PUSH + Ranking 両方に出た銘柄を優先
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .constants import (
    PUSH_SOURCE,
    RANKING_SOURCE,
)

from .utils import (
    safe_symbol,
    safe_str,
    safe_float,
    normalize_side,
    has_meaningful_value,
)

logger = logging.getLogger(__name__)


def candidate_key(c: Dict[str, Any]) -> Tuple[str, str]:
    """
    同一銘柄・同一売買方向で統合する。

    interval は sources_detail に残す。
    """

    return (
        safe_symbol(c.get("symbol")),
        normalize_side(c.get("side")) or normalize_side(c.get("signal")) or "BUY",
    )


def merge_value_if_missing(
    dst: Dict[str, Any],
    src: Dict[str, Any],
    keys: Iterable[str],
) -> None:
    for key in keys:
        old = dst.get(key)
        new = src.get(key)

        if not has_meaningful_value(old) and has_meaningful_value(new):
            dst[key] = new


def append_source_detail(g: Dict[str, Any], c: Dict[str, Any]) -> None:
    source = safe_str(c.get("source"))
    interval = safe_str(c.get("interval"))

    detail = {
        "source": source,
        "interval": interval,
        "entry_score": safe_float(c.get("entry_score")),
        "score": safe_float(c.get("score")),
        "score_buy": safe_float(c.get("score_buy")),
        "score_sell": safe_float(c.get("score_sell")),
        "score_total": safe_float(c.get("score_total")),
        "final_score": safe_float(c.get("final_score")),
        "display_score": safe_float(c.get("display_score")),
        "slope": safe_float(c.get("slope")),
        "score_slope": safe_float(c.get("score_slope")),
        "score_mtf": safe_float(c.get("score_mtf")),
        "rsi": safe_float(c.get("rsi")),
        "macd": safe_float(c.get("macd")),
        "ranking_type": safe_str(c.get("ranking_type")),
        "best_rank": safe_float(c.get("best_rank")),
        "datetime": safe_str(c.get("datetime")),
    }

    g.setdefault("sources_detail", [])
    g["sources_detail"].append(detail)


def merge_ai_entry_candidates(
    *,
    push_candidates: Optional[List[Dict[str, Any]]] = None,
    ranking_candidates: Optional[List[Dict[str, Any]]] = None,
    max_total: int = 30,
) -> List[Dict[str, Any]]:
    """
    PUSH由来 + ランキング由来を AI-Gate 用に統合する。

    優先:
      1. PUSH と Ranking の両方に出た銘柄
      2. combined_entry_score が高い銘柄
      3. 1min / 3min / 5min 複数足に出た銘柄
    """

    push_candidates = push_candidates or []
    ranking_candidates = ranking_candidates or []

    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}

    all_items = list(push_candidates) + list(ranking_candidates)

    for c in all_items:
        if not isinstance(c, dict):
            continue

        key = candidate_key(c)
        symbol, side = key

        if not symbol:
            continue

        source = safe_str(c.get("source"))

        if key not in grouped:
            base = dict(c)
            base["symbol"] = symbol
            base["side"] = side
            base["signal"] = side
            base["matched_sources"] = []
            base["sources_detail"] = []
            base["has_push_summary"] = False
            base["has_ranking_summary"] = False
            base["combined_entry_score"] = 0.0
            grouped[key] = base

        g = grouped[key]

        if source and source not in g["matched_sources"]:
            g["matched_sources"].append(source)

        if source == PUSH_SOURCE:
            g["has_push_summary"] = True

        if source == RANKING_SOURCE:
            g["has_ranking_summary"] = True

        append_source_detail(g, c)

        g["combined_entry_score"] = (
            safe_float(g.get("combined_entry_score"))
            + safe_float(c.get("entry_score"))
        )

        merge_value_if_missing(
            g,
            c,
            [
                "symbolname",
                "current_price",
                "close",
                "datetime",
                "score",
                "score_buy",
                "score_sell",
                "score_total",
                "final_score",
                "display_score",
                "slope",
                "slope_atr_scaled",
                "score_slope",
                "mtf",
                "score_mtf",
                "mtf_score",
                "rsi",
                "macd",
                "signal_value",
                "ranking_type",
                "rank_type",
                "best_rank",
                "rank",
                "rank_history",
                "hist",
            ],
        )

    merged = list(grouped.values())

    for c in merged:
        matched_sources = c.get("matched_sources") or []
        details = c.get("sources_detail") or []

        has_push = bool(c.get("has_push_summary"))
        has_ranking = bool(c.get("has_ranking_summary"))

        both_bonus = 30.0 if has_push and has_ranking else 0.0

        intervals = [
            safe_str(x.get("interval"))
            for x in details
            if isinstance(x, dict)
        ]

        interval_bonus = 0.0

        if "1min" in intervals:
            interval_bonus += 5.0

        if "3min" in intervals:
            interval_bonus += 3.0

        if "5min" in intervals:
            interval_bonus += 2.0

        multi_interval_bonus = max(0, len(set(intervals)) - 1) * 2.0
        source_count_bonus = max(0, len(set(matched_sources)) - 1) * 5.0

        c["ai_priority_score"] = (
            safe_float(c.get("combined_entry_score"))
            + both_bonus
            + interval_bonus
            + multi_interval_bonus
            + source_count_bonus
        )

        if has_push and has_ranking:
            c["entry_priority_reason"] = "PUSHサマリーとランキング由来サマリーの両方で候補化"
        elif has_push:
            c["entry_priority_reason"] = "PUSHサマリーで候補化"
        elif has_ranking:
            c["entry_priority_reason"] = "ランキング由来サマリーで候補化"
        else:
            c["entry_priority_reason"] = "候補化"

    merged = sorted(
        merged,
        key=lambda x: safe_float(x.get("ai_priority_score")),
        reverse=True,
    )

    if max_total and max_total > 0:
        merged = merged[: int(max_total)]

    logger.info(
        "[TOP MERGER] merged total=%d push=%d ranking=%d both=%d",
        len(merged),
        sum(1 for x in merged if x.get("has_push_summary")),
        sum(1 for x in merged if x.get("has_ranking_summary")),
        sum(
            1
            for x in merged
            if x.get("has_push_summary") and x.get("has_ranking_summary")
        ),
    )

    return merged