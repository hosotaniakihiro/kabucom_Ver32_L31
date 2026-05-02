# ============================================================
# File   : trading/entry/pipeline/candidate_bridge.py
# Function:
#   - summary df / AI candidate dict を pending entry に変換
#   - PUSH由来 + ランキング由来の統合候補収集
#   - pending_entries への橋渡し
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-CANDIDATE-BRIDGE
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .constants import (
    SUMMARY_BUY_TOP_N,
    SUMMARY_SELL_TOP_N,
    AI_ENTRY_TOP_N,
    AI_ENTRY_MAX_TOTAL,
    AI_ENTRY_INTERVALS_DEFAULT,
    AI_ENTRY_SIDES_DEFAULT,
    SOURCE_PUSH_SUMMARY,
    SOURCE_RANKING_SUMMARY,
)

from .imports import (
    global_data,
    prepare_buy_sell_top_df,
    collect_ai_entry_candidates,
    log_ai_entry_candidates,
)

from .guards import (
    pass_symbol_guards,
)

from .pending_bridge import (
    append_entries_to_pending,
)

from .utils import (
    safe_str,
    safe_float,
    safe_int,
    safe_bool,
    safe_symbol,
    safe_copy_df,
    normalize_side,
    interval_label_to_int,
    candidate_score,
    first_non_empty,
)

logger = logging.getLogger(__name__)


# ============================================================
# summary row helpers
# ============================================================

def resolve_side_from_row(row: pd.Series) -> Optional[str]:
    side = normalize_side(row.get("signal"))

    if side in ("BUY", "SELL"):
        return side

    side = normalize_side(row.get("side"))

    if side in ("BUY", "SELL"):
        return side

    side = normalize_side(row.get("entry_decision"))

    if side in ("BUY", "SELL"):
        return side

    score_buy = safe_float(row.get("score_buy", row.get("score")), 0.0)
    score_sell = safe_float(row.get("score_sell"), 0.0)

    if score_buy <= 0 and score_sell <= 0:
        return None

    if score_sell > score_buy:
        return "SELL"

    return "BUY"


def resolve_reason_from_row(row: pd.Series, interval: int, source_name: str) -> str:
    reason = safe_str(row.get("reason"))

    if reason:
        return reason

    side = resolve_side_from_row(row) or "UNKNOWN"
    score_buy = safe_float(row.get("score_buy", row.get("score")), 0.0)
    score_sell = safe_float(row.get("score_sell"), 0.0)

    return (
        f"{source_name.upper()}_{interval}MIN_{side}"
        f"_score_buy={score_buy:.4f}_score_sell={score_sell:.4f}"
    )


def resolve_entry_score(row: pd.Series, side: str) -> float:
    if side == "SELL":
        return safe_float(row.get("score_sell"), 0.0)

    return safe_float(row.get("score_buy", row.get("score")), 0.0)


def make_summary_entry(row: pd.Series, interval: int, source_name: str) -> Optional[dict]:
    """
    legacy summary df row -> pending entry.
    """

    try:
        symbol = safe_symbol(row.get("symbol"))

        if not symbol:
            return None

        side = resolve_side_from_row(row)

        if side not in ("BUY", "SELL"):
            return None

        ok, reason_ng = pass_symbol_guards(
            symbol=symbol,
            side=side,
            source="SUMMARY",
            log_prefix="[SUMMARY->PENDING]",
        )

        if not ok:
            logger.debug(
                "[SUMMARY->PENDING] blocked symbol=%s side=%s reason=%s",
                symbol,
                side,
                reason_ng,
            )
            return None

        score = resolve_entry_score(row, side)
        reason = resolve_reason_from_row(row, interval, source_name)

        symbol_name_map = getattr(global_data, "symbol_name_map", {}) or {}

        symbolname = safe_str(
            first_non_empty(
                row.get("symbolname"),
                row.get("name"),
                symbol_name_map.get(symbol, ""),
            )
        )

        close_price = safe_float(
            first_non_empty(
                row.get("close"),
                row.get("close_price"),
                row.get("current_price"),
                row.get("price"),
                0.0,
            ),
            0.0,
        )

        entry = {
            "symbol": symbol,
            "symbolname": symbolname,
            "side": side,
            "entry_decision": side,
            "source": source_name,
            "entry_source": source_name,
            "entry_type": "SUMMARY_AI",
            "interval": safe_int(interval, interval),
            "reason": reason,
            "score": score,
            "score_buy": safe_float(row.get("score_buy", row.get("score")), 0.0),
            "score_sell": safe_float(row.get("score_sell"), 0.0),
            "score_total": safe_float(row.get("score_total"), 0.0),
            "final_score": safe_float(row.get("final_score"), 0.0),
            "display_score": safe_float(row.get("display_score"), 0.0),
            "close": close_price,
            "price": close_price,
            "volume": safe_float(row.get("volume"), 0.0),
            "datetime": safe_str(row.get("datetime")),
            "date": safe_str(row.get("date")),
            "start_time": safe_str(row.get("start_time")),
            "end_time": safe_str(row.get("end_time")),
            "signal": side,
            "ai_allow": safe_bool(row.get("ai_allow"), False),
            "ai_reason": safe_str(row.get("ai_reason")),
            "confidence": safe_float(row.get("confidence"), 0.0),

            # indicators
            "slope": safe_float(row.get("slope"), 0.0),
            "score_slope": safe_float(row.get("score_slope"), 0.0),
            "score_mtf": safe_float(row.get("score_mtf"), 0.0),
            "mtf_score": safe_float(row.get("mtf_score"), 0.0),
            "rsi": safe_float(row.get("rsi"), 0.0),
            "macd": safe_float(row.get("macd"), 0.0),
            "signal_value": safe_float(row.get("signal_value"), 0.0),
        }

        return entry

    except Exception:
        logger.exception("[SUMMARY->PENDING] make entry failed")
        return None


def make_ai_candidate_entry(candidate: Dict[str, Any]) -> Optional[dict]:
    """
    collect_ai_entry_candidates() の候補 dict -> pending entry。

    対象:
      - PUSH由来サマリー候補
      - ランキング由来サマリー候補
      - PUSH + Ranking 統合候補
    """

    try:
        if not isinstance(candidate, dict):
            return None

        symbol = safe_symbol(candidate.get("symbol"))

        if not symbol:
            return None

        side = normalize_side(
            candidate.get("side")
            or candidate.get("signal")
            or candidate.get("entry_decision")
        )

        if side not in ("BUY", "SELL"):
            return None

        source = safe_str(candidate.get("source")) or "ai_candidates"
        matched_sources = candidate.get("matched_sources") or []

        if isinstance(matched_sources, str):
            matched_sources = [matched_sources]

        has_push_summary = safe_bool(candidate.get("has_push_summary"), False)
        has_ranking_summary = safe_bool(candidate.get("has_ranking_summary"), False)

        if SOURCE_PUSH_SUMMARY in matched_sources:
            has_push_summary = True

        if SOURCE_RANKING_SUMMARY in matched_sources:
            has_ranking_summary = True

        position_source = "AI_CANDIDATES"

        if has_push_summary and has_ranking_summary:
            position_source = "SUMMARY_RANKING"
        elif has_push_summary:
            position_source = "SUMMARY"
        elif has_ranking_summary:
            position_source = "RANKING"

        ok, reason_ng = pass_symbol_guards(
            symbol=symbol,
            side=side,
            source=position_source,
            log_prefix="[AI-CANDIDATE->PENDING]",
        )

        if not ok:
            logger.debug(
                "[AI-CANDIDATE->PENDING] blocked symbol=%s side=%s source=%s reason=%s",
                symbol,
                side,
                position_source,
                reason_ng,
            )
            return None

        interval = interval_label_to_int(candidate.get("interval"), 0)

        if interval <= 0:
            details = candidate.get("sources_detail") or []

            if isinstance(details, list) and details:
                for d in details:
                    if isinstance(d, dict):
                        interval = interval_label_to_int(d.get("interval"), 0)

                        if interval > 0:
                            break

        if interval <= 0:
            interval = 1

        score = candidate_score(candidate)

        current_price = safe_float(
            first_non_empty(
                candidate.get("current_price"),
                candidate.get("close"),
                candidate.get("price"),
                0.0,
            ),
            0.0,
        )

        reason = safe_str(candidate.get("entry_priority_reason"))

        if not reason:
            reason = (
                f"AI_CANDIDATE_{interval}MIN_{side}"
                f"_sources={matched_sources or [source]}"
                f"_score={score:.4f}"
            )

        entry_type = "AI_GATE_CANDIDATE"

        if has_push_summary and has_ranking_summary:
            entry_type = "PUSH_RANKING_SUMMARY_AI"
        elif has_push_summary:
            entry_type = "PUSH_SUMMARY_AI"
        elif has_ranking_summary:
            entry_type = "RANKING_SUMMARY_AI"

        entry = {
            "symbol": symbol,
            "symbolname": safe_str(candidate.get("symbolname")),
            "side": side,
            "entry_decision": side,

            "source": source,
            "entry_source": source,
            "matched_sources": matched_sources,
            "has_push_summary": has_push_summary,
            "has_ranking_summary": has_ranking_summary,
            "sources_detail": candidate.get("sources_detail") or [],

            "entry_type": entry_type,
            "interval": interval,
            "reason": reason,

            "score": score,
            "entry_score": safe_float(candidate.get("entry_score"), 0.0),
            "combined_entry_score": safe_float(candidate.get("combined_entry_score"), 0.0),
            "ai_priority_score": safe_float(candidate.get("ai_priority_score"), score),

            "score_buy": safe_float(candidate.get("score_buy"), 0.0),
            "score_sell": safe_float(candidate.get("score_sell"), 0.0),
            "score_total": safe_float(candidate.get("score_total"), 0.0),
            "final_score": safe_float(candidate.get("final_score"), 0.0),
            "display_score": safe_float(candidate.get("display_score"), 0.0),

            "close": current_price,
            "price": current_price,
            "current_price": current_price,
            "volume": safe_float(candidate.get("volume"), 0.0),

            "datetime": safe_str(candidate.get("datetime")),
            "date": safe_str(candidate.get("date")),
            "start_time": safe_str(candidate.get("start_time")),
            "end_time": safe_str(candidate.get("end_time")),
            "signal": side,

            "ai_allow": safe_bool(candidate.get("ai_allow"), False),
            "ai_reason": safe_str(candidate.get("ai_reason")),
            "confidence": safe_float(candidate.get("confidence"), 0.0),

            # indicators
            "slope": safe_float(candidate.get("slope"), 0.0),
            "slope_atr_scaled": safe_float(candidate.get("slope_atr_scaled"), 0.0),
            "score_slope": safe_float(candidate.get("score_slope"), 0.0),
            "mtf": safe_float(candidate.get("mtf"), 0.0),
            "score_mtf": safe_float(candidate.get("score_mtf"), 0.0),
            "mtf_score": safe_float(candidate.get("mtf_score"), 0.0),
            "rsi": safe_float(candidate.get("rsi"), 0.0),
            "macd": safe_float(candidate.get("macd"), 0.0),
            "signal_value": safe_float(candidate.get("signal_value"), 0.0),

            # ranking fields
            "ranking_type": safe_str(candidate.get("ranking_type")),
            "rank_type": safe_str(candidate.get("rank_type")),
            "best_rank": safe_float(candidate.get("best_rank"), 0.0),
            "rank": safe_float(candidate.get("rank"), 0.0),
            "rank_history": safe_str(candidate.get("rank_history")),
            "hist": safe_str(candidate.get("hist")),
        }

        return entry

    except Exception:
        logger.exception("[AI-CANDIDATE->PENDING] make entry failed")
        return None


def build_pending_entries_from_summary_df(
    df: pd.DataFrame,
    interval: int,
    source_name: str = "summary",
) -> int:
    """
    summary_controller.diff_update(interval) の結果から
    BUY TOP10 / SELL TOP10 を明示抽出して pending_entries に変換する。
    """

    try:
        df = safe_copy_df(df)

        if df.empty:
            logger.debug("[SUMMARY->PENDING] input empty interval=%s", interval)
            return 0

        if "symbol" not in df.columns:
            logger.warning("[SUMMARY->PENDING] symbol column missing interval=%s", interval)
            return 0

        buy_df, sell_df = prepare_buy_sell_top_df(
            df,
            buy_top_n=SUMMARY_BUY_TOP_N,
            sell_top_n=SUMMARY_SELL_TOP_N,
        )

        target_df = pd.concat([buy_df, sell_df], ignore_index=True, sort=False)

        if target_df.empty:
            logger.info("[SUMMARY->PENDING] no BUY/SELL top candidates interval=%s", interval)
            return 0

        entries: List[dict] = []

        for _, row in target_df.iterrows():
            entry = make_summary_entry(row, interval=interval, source_name=source_name)

            if entry:
                entries.append(entry)

        logger.info(
            "[SUMMARY->PENDING] interval=%s top_rows=%d valid_entries=%d",
            interval,
            len(target_df),
            len(entries),
        )

        return append_entries_to_pending(
            entries,
            log_prefix=f"[SUMMARY->PENDING] interval={interval}",
        )

    except Exception:
        logger.exception("[SUMMARY->PENDING] build failed interval=%s", interval)
        return 0


def build_pending_entries_from_ai_candidates(
    candidates: List[Dict[str, Any]],
    *,
    interval: Optional[int] = None,
) -> int:
    """
    collect_ai_entry_candidates() の結果を pending_entries に変換する。

    ここで PUSH由来 + ランキング由来の候補が pending_entries に入る。
    """

    try:
        candidates = candidates or []

        if not candidates:
            logger.info("[AI-CANDIDATE->PENDING] no candidates interval=%s", interval)
            return 0

        entries: List[dict] = []

        for candidate in candidates:
            entry = make_ai_candidate_entry(candidate)

            if entry:
                entries.append(entry)

        push_count = sum(1 for x in entries if x.get("has_push_summary"))
        ranking_count = sum(1 for x in entries if x.get("has_ranking_summary"))
        both_count = sum(
            1
            for x in entries
            if x.get("has_push_summary") and x.get("has_ranking_summary")
        )

        logger.info(
            "[AI-CANDIDATE->PENDING] interval=%s candidates=%d valid_entries=%d push=%d ranking=%d both=%d",
            interval,
            len(candidates),
            len(entries),
            push_count,
            ranking_count,
            both_count,
        )

        return append_entries_to_pending(
            entries,
            log_prefix=f"[AI-CANDIDATE->PENDING] interval={interval}",
        )

    except Exception:
        logger.exception("[AI-CANDIDATE->PENDING] build failed interval=%s", interval)
        return 0


def resolve_candidate_intervals(interval: Optional[int]) -> Tuple[int, ...]:
    """
    interval 指定があればその足のみ。
    interval None の場合は 1/3/5 すべて。
    """

    if interval is None:
        return tuple(AI_ENTRY_INTERVALS_DEFAULT)

    interval_int = safe_int(interval, 0)

    if interval_int <= 0:
        return tuple(AI_ENTRY_INTERVALS_DEFAULT)

    return (interval_int,)


def collect_integrated_ai_candidates(
    *,
    interval: Optional[int],
    include_push: bool = True,
    include_ranking: bool = True,
) -> List[Dict[str, Any]]:
    """
    PUSH由来 + ランキング由来の AI 候補を統合収集する。
    """

    try:
        if collect_ai_entry_candidates is None:
            logger.warning("[AI CANDIDATES] collect_ai_entry_candidates unavailable")
            return []

        intervals = resolve_candidate_intervals(interval)

        candidates = collect_ai_entry_candidates(
            intervals=intervals,
            top_n=AI_ENTRY_TOP_N,
            max_total=AI_ENTRY_MAX_TOTAL,
            sides=AI_ENTRY_SIDES_DEFAULT,
            include_push=include_push,
            include_ranking=include_ranking,
            drop_fund_etf=True,
        )

        if log_ai_entry_candidates is not None:
            try:
                log_ai_entry_candidates(
                    candidates,
                    prefix="[ENTRY PIPELINE] AI-Gate candidates",
                    limit=10,
                )
            except Exception:
                logger.exception("[ENTRY PIPELINE] AI candidate logging failed")

        push_count = sum(1 for x in candidates if x.get("has_push_summary"))
        ranking_count = sum(1 for x in candidates if x.get("has_ranking_summary"))
        both_count = sum(
            1
            for x in candidates
            if x.get("has_push_summary") and x.get("has_ranking_summary")
        )

        logger.info(
            "[ENTRY PIPELINE] collected AI candidates interval=%s total=%d push=%d ranking=%d both=%d",
            interval,
            len(candidates),
            push_count,
            ranking_count,
            both_count,
        )

        return candidates

    except Exception:
        logger.exception("[ENTRY PIPELINE] collect integrated AI candidates failed")
        return []