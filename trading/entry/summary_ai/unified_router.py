# ============================================================
# File   : trading/entry/summary_ai/unified_router.py
# Version: PRODUCTION-STABLE-REV1.0-4ROUTE-UNIFIED
# Purpose:
#   4ルートを統合してAI gate前の候補リストを作る
#
# Routes:
#   1. RANKING_SUMMARY
#   2. PUSH_SUMMARY
#   3. YAHOO_SUMMARY
#   4. TONOSAMA
#
# Important:
#   - このファイルは発注しない
#   - entry_pipelineを直接呼ばない
#   - AI gateへ渡す候補だけ作る
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Sequence

import pandas as pd

from .unified_candidate import (
    UnifiedEntryCandidate,
    candidate_from_row,
)
from .unified_entry_guard import can_send_to_ai_gate

logger = logging.getLogger(__name__)


def _iter_df_rows(df: pd.DataFrame | None):
    if df is None or df.empty:
        return
    for _, row in df.iterrows():
        yield row


def build_candidates_from_df(
    df: pd.DataFrame | None,
    *,
    source: str,
) -> list[UnifiedEntryCandidate]:
    out: list[UnifiedEntryCandidate] = []

    if df is None or df.empty:
        return out

    for row in _iter_df_rows(df):
        try:
            c = candidate_from_row(row, source=source)
            if c.symbol:
                out.append(c)
        except Exception:
            logger.exception(
                "[UNIFIED ROUTER] failed to convert row source=%s",
                source,
            )

    return out


def merge_candidates(
    candidates: Sequence[UnifiedEntryCandidate],
) -> list[UnifiedEntryCandidate]:
    """
    symbol単位で統合する。

    方針:
      - PUSH/Yahooの本物テクニカルを持つ候補を優先
      - ranking/tonosama情報は reasons/source に残す
      - 同一symbolで最もpriorityが高いものをベースにする
    """

    by_symbol: dict[str, list[UnifiedEntryCandidate]] = {}

    for c in candidates:
        if not c.symbol:
            continue
        by_symbol.setdefault(c.symbol, []).append(c)

    merged: list[UnifiedEntryCandidate] = []

    for symbol, items in by_symbol.items():
        items_sorted = sorted(items, key=lambda x: x.priority, reverse=True)
        base = items_sorted[0]

        sources = []
        reasons = []

        ranking_score = base.ranking_score
        ranking_momentum = base.ranking_momentum
        price_delta_pct = base.price_delta_pct
        rank_improve = base.rank_improve
        volume_delta = base.volume_delta
        tonosama_score = base.tonosama_score
        tonosama_hit = base.tonosama_hit

        for x in items_sorted:
            sources.append(x.source)

            ranking_score = max(ranking_score, x.ranking_score)
            ranking_momentum = max(ranking_momentum, x.ranking_momentum)
            price_delta_pct = max(price_delta_pct, x.price_delta_pct)
            rank_improve = max(rank_improve, x.rank_improve)
            volume_delta = max(volume_delta, x.volume_delta)
            tonosama_score = max(tonosama_score, x.tonosama_score)
            tonosama_hit = tonosama_hit or x.tonosama_hit

            if x.is_ranking_like():
                reasons.append("ranking")
            if x.is_push_like():
                reasons.append("push")
            if x.is_yahoo_like():
                reasons.append("yahoo")
            if x.is_tonosama_like():
                reasons.append("tonosama")

        base.source = "+".join(sorted(set(sources)))
        base.reasons = sorted(set(reasons))

        base.ranking_score = ranking_score
        base.ranking_momentum = ranking_momentum
        base.price_delta_pct = price_delta_pct
        base.rank_improve = rank_improve
        base.volume_delta = volume_delta
        base.tonosama_score = tonosama_score
        base.tonosama_hit = tonosama_hit

        merged.append(base)

    merged.sort(key=lambda x: x.priority, reverse=True)
    return merged


def filter_candidates_for_ai(
    candidates: Sequence[UnifiedEntryCandidate],
    *,
    max_candidates: int = 10,
    require_real_technical_for_entry: bool = True,
    min_slope_atr_scaled: float = 0.02,
    min_buy_score: float = 0.0,
    max_sell_score: float = 99.0,
) -> list[UnifiedEntryCandidate]:
    out: list[UnifiedEntryCandidate] = []

    for c in candidates:
        ok, reason = can_send_to_ai_gate(
            c,
            require_real_technical_for_entry=require_real_technical_for_entry,
            min_slope_atr_scaled=min_slope_atr_scaled,
            min_buy_score=min_buy_score,
            max_sell_score=max_sell_score,
        )

        if not ok:
            logger.info(
                "[UNIFIED ROUTER] skip symbol=%s name=%s source=%s reason=%s",
                c.symbol,
                c.symbolname,
                c.source,
                reason,
            )
            continue

        logger.info(
            "[UNIFIED ROUTER] pass symbol=%s name=%s source=%s reason=%s "
            "priority=%.3f slope=%.4f buy=%.2f sell=%.2f ranking=%.3f tonosama=%s",
            c.symbol,
            c.symbolname,
            c.source,
            reason,
            c.priority,
            c.slope_atr_scaled,
            c.score_buy,
            c.score_sell,
            c.ranking_score,
            c.tonosama_hit,
        )

        out.append(c)

        if len(out) >= max_candidates:
            break

    return out


def candidates_to_dataframe(
    candidates: Sequence[UnifiedEntryCandidate],
) -> pd.DataFrame:
    rows = []

    for c in candidates:
        row = dict(c.raw or {})

        row.update(
            {
                "symbol": c.symbol,
                "symbolname": c.symbolname,
                "source": c.source,
                "close": c.close,
                "score_buy": c.score_buy,
                "score_sell": c.score_sell,
                "score_total": c.score_total,
                "final_score": c.final_score,
                "slope": c.slope,
                "slope_atr_scaled": c.slope_atr_scaled,
                "atr": c.atr,
                "rsi": c.rsi,
                "macd": c.macd,
                "ranking_score": c.ranking_score,
                "ranking_momentum": c.ranking_momentum,
                "price_delta_pct": c.price_delta_pct,
                "rank_improve": c.rank_improve,
                "volume_delta": c.volume_delta,
                "tonosama_score": c.tonosama_score,
                "tonosama_hit": c.tonosama_hit,
                "unified_priority": c.priority,
                "unified_reasons": ",".join(c.reasons),
            }
        )

        rows.append(row)

    return pd.DataFrame(rows)


def build_unified_ai_candidates(
    *,
    ranking_summary_df: pd.DataFrame | None = None,
    push_summary_df: pd.DataFrame | None = None,
    yahoo_summary_df: pd.DataFrame | None = None,
    tonosama_df: pd.DataFrame | None = None,
    max_candidates: int = 10,
    require_real_technical_for_entry: bool = True,
    min_slope_atr_scaled: float = 0.02,
    min_buy_score: float = 0.0,
    max_sell_score: float = 99.0,
) -> pd.DataFrame:
    """
    4ルート統合のメイン関数。

    戻り値:
      AI gateへ渡すDataFrame
    """

    all_candidates: list[UnifiedEntryCandidate] = []

    all_candidates += build_candidates_from_df(
        ranking_summary_df,
        source="RANKING_SUMMARY",
    )
    all_candidates += build_candidates_from_df(
        push_summary_df,
        source="PUSH_SUMMARY",
    )
    all_candidates += build_candidates_from_df(
        yahoo_summary_df,
        source="YAHOO_SUMMARY",
    )
    all_candidates += build_candidates_from_df(
        tonosama_df,
        source="TONOSAMA",
    )

    logger.info(
        "[UNIFIED ROUTER] collected ranking=%s push=%s yahoo=%s tonosama=%s total=%s",
        0 if ranking_summary_df is None else len(ranking_summary_df),
        0 if push_summary_df is None else len(push_summary_df),
        0 if yahoo_summary_df is None else len(yahoo_summary_df),
        0 if tonosama_df is None else len(tonosama_df),
        len(all_candidates),
    )

    merged = merge_candidates(all_candidates)

    logger.info(
        "[UNIFIED ROUTER] merged symbols=%s",
        len(merged),
    )

    filtered = filter_candidates_for_ai(
        merged,
        max_candidates=max_candidates,
        require_real_technical_for_entry=require_real_technical_for_entry,
        min_slope_atr_scaled=min_slope_atr_scaled,
        min_buy_score=min_buy_score,
        max_sell_score=max_sell_score,
    )

    out = candidates_to_dataframe(filtered)

    logger.info(
        "[UNIFIED ROUTER] output rows=%s symbols=%s",
        len(out),
        out["symbol"].nunique() if not out.empty and "symbol" in out.columns else 0,
    )

    return out