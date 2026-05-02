# ============================================================
# File   : trading/entry/pipeline/router.py
# Function:
#   - entry pipeline router
#   - source=summary / ranking / combined / ai を振り分ける
#   - summary diff_update
#   - PUSH由来 + ランキング由来 AI候補収集
#   - pending_entries 連携
#   - AI enrich
#   - entry_controller 接続
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-ROUTER
# ------------------------------------------------------------
# ✔ run_entry_pipeline 本体
# ✔ source=None は summary として扱う
# ✔ summary source では legacy summary diff_update と AI統合候補の両方を実行
# ✔ combined source では PUSH + Ranking 統合候補のみ実行
# ✔ ranking source は既存 ranking entry pipeline へ委譲
# ✔ ai source は legacy run_ai_entry へ委譲
# ✔ scheduler 絶対防衛
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

from .constants import (
    SOURCE_SUMMARY,
    SOURCE_RANKING,
    SOURCE_AI,
    SOURCE_COMBINED,
)

from .imports import (
    global_data,
    summary_controller,
    run_ranking_entry_pipeline,
)

from .guards import (
    pass_market_regime_guard,
)

from .candidate_bridge import (
    build_pending_entries_from_summary_df,
    build_pending_entries_from_ai_candidates,
    collect_integrated_ai_candidates,
)

from .ai_stage import (
    run_ai_enrich_and_entry_controller,
)

from .legacy_entry import (
    run_ai_entry,
)

from .utils import (
    safe_int,
    normalize_source,
)

logger = logging.getLogger(__name__)


def run_entry_pipeline(
    interval: Optional[int] = None,
    source: Optional[str] = None,
    *args,
    **kwargs,
) -> Any:
    """
    統合エントリー入口。

    source:
      - summary:
          summary_controller.diff_update(interval) を実行し、
          legacy BUY/SELL TOP10 pending 化に加えて、
          collect_ai_entry_candidates() で PUSH + Ranking 統合候補も pending 化する。

      - ranking:
          既存 ranking entry pipeline を実行する。

      - combined / ai_candidates / all / both:
          diff_update なしで collect_ai_entry_candidates() の統合候補のみ pending 化する。

      - ai:
          legacy run_ai_entry() を実行する。

    source=None:
      - summary として扱う。
      - ranking_pipeline_available=True のときに ranking だけへ流れて
        PUSHサマリー候補が AI に渡らない事故を防ぐ。
    """

    try:
        if source is None:
            source = kwargs.get("source")

        if source is None and args:
            source = args[0]

        if source is None:
            source = SOURCE_SUMMARY

        source = normalize_source(source)

        if not source:
            source = SOURCE_SUMMARY

        logger.info(
            "[ENTRY PIPELINE] called source=%s interval=%s args=%s kwargs_keys=%s",
            source,
            interval,
            args,
            list(kwargs.keys()),
        )

        # ==================================================
        # RANKING ENTRY
        # ==================================================

        if source == SOURCE_RANKING:
            if not pass_market_regime_guard(log_prefix="[ENTRY PIPELINE][RANKING]"):
                return None

            if global_data is None:
                logger.warning("[ENTRY PIPELINE] global_data unavailable")
                return None

            if not getattr(global_data, "ranking_pipeline_available", False):
                logger.info("[ENTRY PIPELINE] ranking pipeline not ready")
                return None

            if run_ranking_entry_pipeline is None:
                logger.warning("[ENTRY PIPELINE] ranking entry not imported")
                return None

            try:
                return run_ranking_entry_pipeline()

            except Exception:
                logger.exception("[ENTRY PIPELINE] ranking entry failed")
                return None

        # ==================================================
        # SUMMARY ENTRY
        # ==================================================

        if source == SOURCE_SUMMARY:
            if interval is None:
                logger.warning("[ENTRY PIPELINE] interval missing")
                return None

            if not pass_market_regime_guard(log_prefix="[ENTRY PIPELINE][SUMMARY]"):
                return None

            interval_int = safe_int(interval, 0)

            if interval_int <= 0:
                logger.warning("[ENTRY PIPELINE] invalid interval=%s", interval)
                return None

            legacy_created = 0
            ai_created = 0

            # --------------------------------------------------
            # 1. 既存の summary diff_update -> BUY/SELL TOP10 pending 化
            # --------------------------------------------------

            try:
                if summary_controller is None:
                    logger.warning("[ENTRY PIPELINE][SUMMARY] summary_controller unavailable")
                else:
                    df = summary_controller.diff_update(interval_int)

                    if df is None or df.empty:
                        logger.debug("[ENTRY PIPELINE] diff_update empty interval=%s", interval_int)
                    else:
                        logger.info(
                            "[ENTRY PIPELINE][SUMMARY] diff_update interval=%s rows=%d",
                            interval_int,
                            len(df),
                        )

                        legacy_created = build_pending_entries_from_summary_df(
                            df=df,
                            interval=interval_int,
                            source_name="summary",
                        )

            except Exception:
                logger.exception("[ENTRY PIPELINE][SUMMARY] diff_update/legacy bridge failed")

            # --------------------------------------------------
            # 2. PUSH由来 + ランキング由来の AI 候補を統合 pending 化
            # --------------------------------------------------

            try:
                candidates = collect_integrated_ai_candidates(
                    interval=interval_int,
                    include_push=True,
                    include_ranking=True,
                )

                ai_created = build_pending_entries_from_ai_candidates(
                    candidates,
                    interval=interval_int,
                )

            except Exception:
                logger.exception("[ENTRY PIPELINE][SUMMARY] integrated AI candidates failed")

            total_created = legacy_created + ai_created

            logger.info(
                "[ENTRY PIPELINE][SUMMARY] pending created interval=%s legacy=%d ai_candidates=%d total=%d",
                interval_int,
                legacy_created,
                ai_created,
                total_created,
            )

            if total_created <= 0:
                logger.info(
                    "[ENTRY PIPELINE][SUMMARY] no pending entries created interval=%s",
                    interval_int,
                )
                return None

            return run_ai_enrich_and_entry_controller(
                pipeline_source="SUMMARY",
                interval=interval_int,
            )

        # ==================================================
        # COMBINED / AI CANDIDATES ENTRY
        # ==================================================

        if source == SOURCE_COMBINED:
            if not pass_market_regime_guard(log_prefix="[ENTRY PIPELINE][COMBINED]"):
                return None

            candidates = collect_integrated_ai_candidates(
                interval=interval,
                include_push=True,
                include_ranking=True,
            )

            created = build_pending_entries_from_ai_candidates(
                candidates,
                interval=interval,
            )

            if created <= 0:
                logger.info("[ENTRY PIPELINE][COMBINED] no pending entries created")
                return None

            return run_ai_enrich_and_entry_controller(
                pipeline_source="COMBINED",
                interval=interval,
            )

        # ==================================================
        # AI ENTRY
        # ==================================================

        if source == SOURCE_AI:
            return run_ai_entry()

        # ==================================================
        # UNKNOWN
        # ==================================================

        logger.warning("[ENTRY PIPELINE] unknown source=%s", source)
        return None

    except Exception:
        logger.exception("[ENTRY PIPELINE] fatal error → ignored")
        return None


def run_summary_ai_entry(interval: Optional[int] = None):
    """
    明示的に summary 経由で AI entry を走らせる互換 alias。
    """

    return run_entry_pipeline(interval=interval, source="summary")


def run_combined_ai_entry(interval: Optional[int] = None):
    """
    PUSH由来 + ランキング由来の統合候補のみで AI entry を走らせる。
    """

    return run_entry_pipeline(interval=interval, source="combined")


__all__ = [
    "run_entry_pipeline",
    "run_summary_ai_entry",
    "run_combined_ai_entry",
]