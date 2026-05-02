# ============================================================
# File   : trading/summary/top_candidates_pkg/collectors.py
# Version: Ver3.0-PRODUCTION-SUMMARY-TOP-CANDIDATES-COLLECTORS
# ------------------------------------------------------------
# Function:
#   - PUSH由来サマリー候補収集
#   - ランキング由来サマリー候補収集
#   - AI-Gate 用統合候補収集
#   - score_reasons 要約列を候補dictへ反映
#   - ログ表示を理由付きで強化
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from .constants import (
    DEFAULT_INTERVALS,
    DEFAULT_SIDES,
    PUSH_SOURCE,
    RANKING_SOURCE,
)

from .utils import (
    interval_to_int,
    normalize_side,
)

from .filters import (
    drop_fund_etf_like,
)

from .resolvers import (
    get_push_summary_df,
    get_ranking_summary_df,
)

from .converters import (
    prepare_top_rows_for_side,
    row_to_ai_candidate,
)

from .merger import (
    merge_ai_entry_candidates,
)

from .reason_utils import (
    attach_score_reason_columns,
    build_candidate_reason_payload,
    build_candidate_log_line,
)

logger = logging.getLogger(__name__)


# ============================================================
# internal helpers
# ============================================================

def _attach_reason_columns_if_needed(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """
    score_reasons 由来の表示列を安全付与する。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    try:
        return attach_score_reason_columns(df)
    except Exception:
        logger.debug(
            "[TOP COLLECTOR] attach_score_reason_columns failed",
            exc_info=True,
        )
        return df


def _merge_reason_payload(
    candidate: Dict[str, Any],
    row: pd.Series,
) -> Dict[str, Any]:
    """
    row 側の score_reason 情報を candidate dict へ安全にマージする。
    既存キーはなるべく保持しつつ、空なら補完する。
    """
    try:
        reason_payload = build_candidate_reason_payload(row)
    except Exception:
        logger.debug("[TOP COLLECTOR] build_candidate_reason_payload failed", exc_info=True)
        return candidate

    if not isinstance(candidate, dict):
        candidate = {}

    merged = dict(candidate)

    # 補完したい重要キー
    preferred_keys = [
        "score_reason_top3",
        "score_reason_top5",
        "score_reason_summary",
        "entry_setup_type",
        "pullback_subtype",
        "setup_score",
        "entry_score_v4",
        "final_score",
        "score_buy",
        "score_sell",
        "score_total",
        "score_mtf",
        "score_slope",
        "danger_penalty_score",
        "entry_timing_score",
        "pullback_score_v2",
        "breakout_score",
        "reversal_score",
        "trend_continuation_score",
        "vwap_reclaim_score",
        "range_break_score",
        "retest_success_score",
        "opening_range_break_score",
        "multi_tf_resonance_score",
        "relative_strength_score",
        "phase_shift_score",
        "ranking_persistence_score",
        "fakeout_reversal_score",
        "gap_go_score",
        "volatility_squeeze_score",
    ]

    for key in preferred_keys:
        if key not in reason_payload:
            continue

        current = merged.get(key)
        if current in (None, "", [], {}, 0, 0.0):
            merged[key] = reason_payload.get(key)

    # symbol / symbolname / close / datetime は row_to_ai_candidate 側が優先
    # ただし欠損時のみ補完
    for key in ("symbol", "symbolname", "close", "datetime"):
        if key in reason_payload and merged.get(key) in (None, "", 0, 0.0):
            merged[key] = reason_payload.get(key)

    return merged


def _log_top_rows(
    top_df: pd.DataFrame,
    *,
    source: str,
    interval: int,
    side: str,
    max_rows: int = 3,
) -> None:
    """
    デバッグ/運用ログ用に上位数件を score reasons 付きで出す。
    """
    if top_df is None or top_df.empty:
        return

    try:
        head_df = top_df.head(max_rows)
        for _, row in head_df.iterrows():
            line = build_candidate_log_line(row)
            logger.info(
                "[TOP COLLECTOR] source=%s interval=%s side=%s %s",
                source,
                interval,
                side,
                line,
            )
    except Exception:
        logger.debug(
            "[TOP COLLECTOR] top row logging failed source=%s interval=%s side=%s",
            source,
            interval,
            side,
            exc_info=True,
        )


# ============================================================
# main
# ============================================================

def collect_push_summary_candidates(
    *,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
    top_n: int = 10,
    sides: Iterable[str] = DEFAULT_SIDES,
    drop_fund_etf: bool = True,
) -> List[Dict[str, Any]]:
    """
    PUSH由来サマリーから AI-Gate 用候補を収集する。

    Returns:
      List[dict]
    """

    candidates: List[Dict[str, Any]] = []

    for interval in intervals:
        interval_int = interval_to_int(interval)

        if interval_int <= 0:
            logger.warning("[TOP COLLECTOR] invalid push interval=%s", interval)
            continue

        df = get_push_summary_df(interval_int)

        if df is None or df.empty:
            logger.warning(
                "[TOP COLLECTOR] push summary empty interval=%s",
                interval_int,
            )
            continue

        if drop_fund_etf:
            before = len(df)
            df = drop_fund_etf_like(df)
            after = len(df)

            if before != after:
                logger.info(
                    "[TOP COLLECTOR] push fund/etf dropped interval=%s rows=%d",
                    interval_int,
                    before - after,
                )

        if "symbol" not in df.columns:
            logger.warning(
                "[TOP COLLECTOR] push summary missing symbol interval=%s cols=%s",
                interval_int,
                list(df.columns),
            )
            continue

        df = _attach_reason_columns_if_needed(df)

        for side in sides:
            side_norm = normalize_side(side)

            if side_norm not in ("BUY", "SELL"):
                continue

            top_df = prepare_top_rows_for_side(
                df,
                side=side_norm,
                top_n=top_n,
            )

            if top_df.empty:
                logger.info(
                    "[TOP COLLECTOR] push top empty interval=%s side=%s",
                    interval_int,
                    side_norm,
                )
                continue

            top_df = _attach_reason_columns_if_needed(top_df)
            _log_top_rows(
                top_df,
                source=PUSH_SOURCE,
                interval=interval_int,
                side=side_norm,
                max_rows=min(3, top_n),
            )

            for _, row in top_df.iterrows():
                c = row_to_ai_candidate(
                    row,
                    side=side_norm,
                    source=PUSH_SOURCE,
                    interval=interval_int,
                )

                c = _merge_reason_payload(c, row)

                if c.get("symbol"):
                    candidates.append(c)

    logger.info(
        "[TOP COLLECTOR] push collected total=%d",
        len(candidates),
    )

    return candidates


def collect_ranking_summary_candidates(
    *,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
    top_n: int = 10,
    sides: Iterable[str] = DEFAULT_SIDES,
    drop_fund_etf: bool = True,
) -> List[Dict[str, Any]]:
    """
    ランキング由来サマリーから AI-Gate 用候補を収集する。

    Returns:
      List[dict]
    """

    candidates: List[Dict[str, Any]] = []

    for interval in intervals:
        interval_int = interval_to_int(interval)

        if interval_int <= 0:
            logger.warning("[TOP COLLECTOR] invalid ranking interval=%s", interval)
            continue

        df = get_ranking_summary_df(interval_int)

        if df is None or df.empty:
            logger.warning(
                "[TOP COLLECTOR] ranking summary empty interval=%s",
                interval_int,
            )
            continue

        if drop_fund_etf:
            before = len(df)
            df = drop_fund_etf_like(df)
            after = len(df)

            if before != after:
                logger.info(
                    "[TOP COLLECTOR] ranking fund/etf dropped interval=%s rows=%d",
                    interval_int,
                    before - after,
                )

        if "symbol" not in df.columns:
            logger.warning(
                "[TOP COLLECTOR] ranking summary missing symbol interval=%s cols=%s",
                interval_int,
                list(df.columns),
            )
            continue

        df = _attach_reason_columns_if_needed(df)

        for side in sides:
            side_norm = normalize_side(side)

            if side_norm not in ("BUY", "SELL"):
                continue

            top_df = prepare_top_rows_for_side(
                df,
                side=side_norm,
                top_n=top_n,
            )

            if top_df.empty:
                logger.info(
                    "[TOP COLLECTOR] ranking top empty interval=%s side=%s",
                    interval_int,
                    side_norm,
                )
                continue

            # ranking側は best_rank が小さいほど良いので軽く補正
            if "best_rank" in top_df.columns:
                try:
                    best_rank = pd.to_numeric(
                        top_df["best_rank"],
                        errors="coerce",
                    ).fillna(999.0)

                    rank_bonus = (100.0 - best_rank).clip(lower=0.0, upper=100.0) * 0.05

                    top_df["_entry_score"] = pd.to_numeric(
                        top_df["_entry_score"],
                        errors="coerce",
                    ).fillna(0.0) + rank_bonus

                except Exception:
                    logger.debug(
                        "[TOP COLLECTOR] ranking best_rank bonus failed interval=%s side=%s",
                        interval_int,
                        side_norm,
                        exc_info=True,
                    )

            top_df = _attach_reason_columns_if_needed(top_df)
            _log_top_rows(
                top_df,
                source=RANKING_SOURCE,
                interval=interval_int,
                side=side_norm,
                max_rows=min(3, top_n),
            )

            for _, row in top_df.iterrows():
                c = row_to_ai_candidate(
                    row,
                    side=side_norm,
                    source=RANKING_SOURCE,
                    interval=interval_int,
                )

                c = _merge_reason_payload(c, row)

                if c.get("symbol"):
                    candidates.append(c)

    logger.info(
        "[TOP COLLECTOR] ranking collected total=%d",
        len(candidates),
    )

    return candidates


def collect_ai_entry_candidates(
    *,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
    top_n: int = 10,
    max_total: int = 30,
    sides: Iterable[str] = DEFAULT_SIDES,
    include_push: bool = True,
    include_ranking: bool = True,
    drop_fund_etf: bool = True,
) -> List[Dict[str, Any]]:
    """
    AI-Gate に渡すための統合候補を返す。

    取得対象:
      - PUSH由来 summary
      - ランキング由来 summary

    Returns:
      List[dict]
    """

    push_candidates: List[Dict[str, Any]] = []
    ranking_candidates: List[Dict[str, Any]] = []

    if include_push:
        push_candidates = collect_push_summary_candidates(
            intervals=intervals,
            top_n=top_n,
            sides=sides,
            drop_fund_etf=drop_fund_etf,
        )

    if include_ranking:
        ranking_candidates = collect_ranking_summary_candidates(
            intervals=intervals,
            top_n=top_n,
            sides=sides,
            drop_fund_etf=drop_fund_etf,
        )

    merged = merge_ai_entry_candidates(
        push_candidates=push_candidates,
        ranking_candidates=ranking_candidates,
        max_total=max_total,
    )

    logger.info(
        "[TOP COLLECTOR] final source_counts push=%d ranking=%d both=%d total=%d",
        sum(1 for x in merged if x.get("has_push_summary")),
        sum(1 for x in merged if x.get("has_ranking_summary")),
        sum(
            1
            for x in merged
            if x.get("has_push_summary") and x.get("has_ranking_summary")
        ),
        len(merged),
    )

    # 上位数件だけ理由付きで見えるようにする
    try:
        for item in merged[: min(5, len(merged))]:
            logger.info(
                "[TOP COLLECTOR] merged symbol=%s side=%s interval=%s source=%s score=%.2f reasons=%s",
                item.get("symbol", ""),
                item.get("side", ""),
                item.get("interval", ""),
                item.get("source", ""),
                float(item.get("entry_score_v4") or item.get("_entry_score") or item.get("entry_score") or 0.0),
                item.get("score_reason_summary", ""),
            )
    except Exception:
        logger.debug("[TOP COLLECTOR] merged log summary failed", exc_info=True)

    return merged


def collect_top_candidates_for_ai(
    *,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
    top_n: int = 10,
    max_total: int = 30,
    sides: Iterable[str] = DEFAULT_SIDES,
) -> List[Dict[str, Any]]:
    """
    互換用 alias。
    collect_ai_entry_candidates() と同じ。
    """

    return collect_ai_entry_candidates(
        intervals=intervals,
        top_n=top_n,
        max_total=max_total,
        sides=sides,
    )